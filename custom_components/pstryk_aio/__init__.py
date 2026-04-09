"""Integracja Pstryk AIO."""
import asyncio
import logging
from datetime import datetime, timedelta

from typing import Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY 
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import PstrykApiClientApiKey, PstrykApiError, PstrykAuthError 
from .const import (
    DOMAIN,
    PLATFORMS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    COORDINATOR_KEY_MAIN,
    KEY_METER_DATA_USAGE,
    KEY_METER_DATA_COST,
    KEY_PRICING_DATA_PURCHASE_TODAY,
    KEY_PRICING_DATA_PURCHASE_TOMORROW,
    KEY_PRICING_DATA_PROSUMER_TODAY,
    KEY_PRICING_DATA_PROSUMER_TOMORROW,
    KEY_LAST_UPDATE,
    STORAGE_KEY_PRICES,
    STORAGE_VERSION_PRICES,
    ATTR_UPDATE_STATUS,
    ATTR_ERROR_MESSAGE,
    ATTR_UPDATE_DETAILS,
)

_LOGGER = logging.getLogger(__name__)


def _has_meaningful_price_data(response_data: Optional[dict]) -> bool:
    if not response_data or not isinstance(response_data.get("frames"), list):
        return False
    if not response_data["frames"]:
        return False
    for frame in response_data["frames"]:
        if frame.get("price_gross") is not None and frame.get("price_gross") != 0.0:
            return True
    return False


def _count_meaningful_frames(response_data: Optional[dict]) -> int:
    """Liczy ramki z ceną różną od zera."""
    if not response_data or not isinstance(response_data.get("frames"), list):
        return 0
    count = 0
    for frame in response_data["frames"]:
        if frame.get("price_gross") is not None and frame.get("price_gross") != 0.0:
            count += 1
    return count


def _is_pricing_data_complete(response_data: Optional[dict]) -> bool:
    """Sprawdza, czy dane są kompletne (co najmniej 23 ramki I co najmniej jedna nie-zero)."""
    if not response_data or not isinstance(response_data.get("frames"), list):
        return False
    if len(response_data["frames"]) < 23:
        return False
    return _has_meaningful_price_data(response_data)


def _is_ultimate_complete(response_data: Optional[dict]) -> bool:
    """Sprawdza, czy dane są idealnie kompletne (24 ramki I 24 nie-zera)."""
    if not response_data or not isinstance(response_data.get("frames"), list):
        return False
    if len(response_data["frames"]) < 23: # Pozwalamy na 23 dla DST
        return False
    meaningful = _count_meaningful_frames(response_data)
    return meaningful >= 23


def _should_accept_new_pricing_data(new_data: Optional[dict], old_data: Optional[dict]) -> bool:
    """Decyduje, czy nowe dane cenowe powinny zastąpić te w cache."""
    if not new_data or not isinstance(new_data.get("frames"), list) or not new_data["frames"]:
        return False
    
    new_frames_count = len(new_data["frames"])
    new_meaningful_count = _count_meaningful_frames(new_data)
    
    # Jeśli cache jest pusty, bierzemy cokolwiek co ma ramki
    if not old_data or not isinstance(old_data.get("frames"), list) or not old_data["frames"]:
        return True
        
    old_frames_count = len(old_data["frames"])
    old_meaningful_count = _count_meaningful_frames(old_data)

    # 1. Ochrona przed "regresją do zer": Jeśli nowe dane to same zera (а stare miały ceny), ignorujemy.
    if new_meaningful_count == 0 and old_meaningful_count > 0:
        return False
        
    # 2. Jeśli przybyło ramek (np. 12 -> 24), to aktualizujemy.
    if new_frames_count > old_frames_count:
        return True
        
    # 3. Główne wymaganie użytkownika: Jeśli cokolwiek się zmieniło w wartościach (np. korekta ceny).
    if new_data != old_data:
        # Nie bierzemy mniej ramek niż mamy obecnie
        if new_frames_count < old_frames_count:
            return False
        return True
        
    return False


def _are_frames_for_expected_date(response_data: Optional[dict], expected_date: datetime.date) -> bool:
    """Sprawdza, czy daty w ramkach odpowiedzi API odpowiadają oczekiwanej dacie."""
    if not response_data or not isinstance(response_data.get("frames"), list) or not response_data["frames"]:
        return False

    first_frame = response_data["frames"][0]
    start_utc_str = first_frame.get("start")
    if not start_utc_str:
        return False

    try:
        start_utc_dt = dt_util.parse_datetime(start_utc_str)
        if not start_utc_dt:
            return False
        start_local_dt = dt_util.as_local(start_utc_dt)
        return start_local_dt.date() == expected_date
    except Exception:
        return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Konfiguracja integracji na podstawie wpisu konfiguracyjnego."""
    api_key = entry.data.get(CONF_API_KEY)
    
    session = async_get_clientsession(hass)
    api_client = PstrykApiClientApiKey(api_key=api_key, session=session) 
    
    # Inicjalizacja Storage dla cen
    store = Store(hass, STORAGE_VERSION_PRICES, STORAGE_KEY_PRICES)
    cached_data = await store.async_load() or {}

    async def async_update_data():
        """Pobiera najnowsze dane z API Pstryk."""
        _LOGGER.debug("Rozpoczynanie aktualizacji danych dla Pstryk AIO")
        
        status = "OK"
        error_msg = None
        update_details = []

        try:
            now_in_ha_tz = dt_util.now()
            current_local_date = now_in_ha_tz.date()
            tomorrow_local_date = current_local_date + timedelta(days=1)
            is_after_13_local = now_in_ha_tz.hour >= 13
            now_utc = dt_util.utcnow()

            # Daty dla zużycia
            start_of_current_month_local = now_in_ha_tz.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start_of_previous_month_local = (start_of_current_month_local - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            meter_data_history_start_utc = dt_util.as_utc(start_of_previous_month_local)
            meter_data_history_end_utc = now_utc
            
            today_start_in_ha_tz = now_in_ha_tz.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = dt_util.as_utc(today_start_in_ha_tz)
            today_end_utc = dt_util.as_utc(today_start_in_ha_tz + timedelta(days=1))
            tomorrow_start_utc = today_end_utc
            tomorrow_end_utc = dt_util.as_utc(today_start_in_ha_tz + timedelta(days=2))

            # Zużycie
            meter_data_usage_response = await api_client.get_integrations_meter_data_usage(
                resolution="hour", window_start=meter_data_history_start_utc, window_end=meter_data_history_end_utc
            )
            update_details.append("Usage: OK" if meter_data_usage_response else "Usage: FAIL")
            
            # Koszty
            meter_data_cost_response = await api_client.get_integrations_meter_data_cost(
                resolution="hour", window_start=meter_data_history_start_utc, window_end=meter_data_history_end_utc
            )
            update_details.append("Cost: OK" if meter_data_cost_response else "Cost: FAIL")

            # Reset cache jutra przy zmianie dnia
            if coordinator._date_prices_tomorrow_valid_for != tomorrow_local_date:
                coordinator._cached_purchase_prices_tomorrow = {}
                coordinator._cached_prosumer_prices_tomorrow = {}
                coordinator._date_prices_tomorrow_valid_for = tomorrow_local_date

            successfully_updated_any_today_prices = False

            # --- Ceny ZAKUPU Сегодня ---
            is_today_purchase_complete = _is_pricing_data_complete(coordinator._cached_purchase_prices_today)
            refresh_today_purchase = (coordinator._date_prices_today_fetched != current_local_date or not is_today_purchase_complete)
            
            pricing_purchase_today_response = coordinator._cached_purchase_prices_today
            if refresh_today_purchase:
                api_resp = await api_client.get_integrations_pricing_data(resolution="hour", window_start=today_start_utc, window_end=today_end_utc)
                if _should_accept_new_pricing_data(api_resp, coordinator._cached_purchase_prices_today):
                    coordinator._cached_purchase_prices_today = api_resp
                    pricing_purchase_today_response = api_resp
                    successfully_updated_any_today_prices = True
                    update_details.append(f"PurchaseToday: OK ({'Complete' if _is_pricing_data_complete(api_resp) else 'Partial'})")
                else:
                    update_details.append("PurchaseToday: CACHE/STALE")
            else:
                update_details.append("PurchaseToday: CACHED")

            # --- Ceny SPRZEDAŻY Сегодня ---
            is_today_prosumer_complete = _is_pricing_data_complete(coordinator._cached_prosumer_prices_today)
            refresh_today_prosumer = (coordinator._date_prices_today_fetched != current_local_date or not is_today_prosumer_complete)

            pricing_prosumer_today_response = coordinator._cached_prosumer_prices_today
            if refresh_today_prosumer:
                api_resp = await api_client.get_integrations_prosumer_pricing_data(resolution="hour", window_start=today_start_utc, window_end=today_end_utc)
                if _should_accept_new_pricing_data(api_resp, coordinator._cached_prosumer_prices_today):
                    coordinator._cached_prosumer_prices_today = api_resp
                    pricing_prosumer_today_response = api_resp
                    successfully_updated_any_today_prices = True
                    update_details.append(f"ProsumerToday: OK ({'Complete' if _is_pricing_data_complete(api_resp) else 'Partial'})")
                else:
                    update_details.append("ProsumerToday: CACHE/STALE")
            else:
                update_details.append("ProsumerToday: CACHED")

            if successfully_updated_any_today_prices and _is_pricing_data_complete(coordinator._cached_purchase_prices_today) and _is_pricing_data_complete(coordinator._cached_prosumer_prices_today):
                coordinator._date_prices_today_fetched = current_local_date

            # --- Ceny ZAKUPU Jutro ---
            pricing_purchase_tomorrow_response = coordinator._cached_purchase_prices_tomorrow
            if not _is_ultimate_complete(coordinator._cached_purchase_prices_tomorrow):
                api_resp = await api_client.get_integrations_pricing_data(resolution="hour", window_start=tomorrow_start_utc, window_end=tomorrow_end_utc)
                if api_resp and api_resp.get("frames") and _are_frames_for_expected_date(api_resp, tomorrow_local_date):
                    if _should_accept_new_pricing_data(api_resp, coordinator._cached_purchase_prices_tomorrow):
                        coordinator._cached_purchase_prices_tomorrow = api_resp
                        pricing_purchase_tomorrow_response = api_resp
                        meaningful = _count_meaningful_frames(api_resp)
                        update_details.append(f"PurchaseTomorrow: OK ({meaningful} val)")
                    else:
                        update_details.append("PurchaseTomorrow: NO_CHANGE")
                else:
                    update_details.append("PurchaseTomorrow: N/A")
            else:
                update_details.append("PurchaseTomorrow: CACHED")

            # --- Ceny SPRZEDAŻY Jutro ---
            pricing_prosumer_tomorrow_response = coordinator._cached_prosumer_prices_tomorrow
            if not _is_ultimate_complete(coordinator._cached_prosumer_prices_tomorrow):
                api_resp = await api_client.get_integrations_prosumer_pricing_data(resolution="hour", window_start=tomorrow_start_utc, window_end=tomorrow_end_utc)
                if api_resp and api_resp.get("frames") and _are_frames_for_expected_date(api_resp, tomorrow_local_date):
                    if _should_accept_new_pricing_data(api_resp, coordinator._cached_prosumer_prices_tomorrow):
                        coordinator._cached_prosumer_prices_tomorrow = api_resp
                        pricing_prosumer_tomorrow_response = api_resp
                        meaningful = _count_meaningful_frames(api_resp)
                        update_details.append(f"ProsumerTomorrow: OK ({meaningful} val)")
                    else:
                        update_details.append("ProsumerTomorrow: NO_CHANGE")
                else:
                    update_details.append("ProsumerTomorrow: N/A")
            else:
                update_details.append("ProsumerTomorrow: CACHED")

            # Finalize responses
            pricing_purchase_today_response = pricing_purchase_today_response or {}
            pricing_prosumer_today_response = pricing_prosumer_today_response or {}
            pricing_purchase_tomorrow_response = pricing_purchase_tomorrow_response or {}
            pricing_prosumer_tomorrow_response = pricing_prosumer_tomorrow_response or {}

            # Save to storage
            await store.async_save({
                "prices_today_purchase": pricing_purchase_today_response,
                "prices_today_prosumer": pricing_prosumer_today_response,
                "prices_tomorrow_purchase": pricing_purchase_tomorrow_response,
                "prices_tomorrow_prosumer": pricing_prosumer_tomorrow_response,
                "date_today": current_local_date.isoformat(),
                "date_tomorrow": tomorrow_local_date.isoformat(),
            })

            return {
                KEY_METER_DATA_USAGE: meter_data_usage_response,
                KEY_METER_DATA_COST: meter_data_cost_response,
                KEY_PRICING_DATA_PURCHASE_TODAY: pricing_purchase_today_response,
                KEY_PRICING_DATA_PURCHASE_TOMORROW: pricing_purchase_tomorrow_response,
                KEY_PRICING_DATA_PROSUMER_TODAY: pricing_prosumer_today_response,
                KEY_PRICING_DATA_PROSUMER_TOMORROW: pricing_prosumer_tomorrow_response,
                KEY_LAST_UPDATE: dt_util.utcnow().isoformat(),
                ATTR_UPDATE_STATUS: "OK",
                ATTR_ERROR_MESSAGE: None,
                ATTR_UPDATE_DETAILS: ", ".join(update_details),
            }

        except Exception as err:
            _LOGGER.exception(f"Unexpected error updating Pstryk AIO: {err}")
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    update_interval_minutes = entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL_MINUTES)
    coordinator = PstrykDataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"pstryk_aio_{entry.entry_id}",
        update_interval=timedelta(minutes=update_interval_minutes),
        update_method=async_update_data,
        cached_data=cached_data
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        COORDINATOR_KEY_MAIN: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


class PstrykDataUpdateCoordinator(DataUpdateCoordinator):
    """Koordynator aktualizacji danych Pstryk AIO."""

    def __init__(self, hass, logger, name, update_interval, update_method, cached_data):
        super().__init__(hass, logger, name=name, update_interval=update_interval, update_method=update_method)
        
        # Inicjalizacja buforów z cache
        self._cached_purchase_prices_today = cached_data.get("prices_today_purchase", {})
        self._cached_prosumer_prices_today = cached_data.get("prices_today_prosumer", {})
        self._cached_purchase_prices_tomorrow = cached_data.get("prices_tomorrow_purchase", {})
        self._cached_prosumer_prices_tomorrow = cached_data.get("prices_tomorrow_prosumer", {})
        
        # Obsługa dat w cache
        self._date_prices_today_fetched = None
        date_today_str = cached_data.get("date_today")
        if date_today_str:
            try:
                self._date_prices_today_fetched = datetime.fromisoformat(date_today_str).date()
            except ValueError:
                self._date_prices_today_fetched = None
                
        self._date_prices_tomorrow_valid_for = None
        date_tomorrow_str = cached_data.get("date_tomorrow")
        if date_tomorrow_str:
            try:
                self._date_prices_tomorrow_valid_for = datetime.fromisoformat(date_tomorrow_str).date()
            except ValueError:
                self._date_prices_tomorrow_valid_for = None


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Odładowanie integracji."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

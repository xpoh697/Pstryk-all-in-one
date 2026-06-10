# DEBATE: Caching Logic for Price Data

## Problem
The integration caches price data for the entire day as soon as any frames are received. If the API returns partial data (e.g., only the first 12 hours), the integration stops trying to fetch the remaining hours for that day, leaving the sensors with incomplete data.

---

## Archi (Lead Architect)
The current logic uses a simple boolean check: "Did we get frames for today? Yes -> Stop fetching today." This is too restrictive. 

**Proposed Solution:**
1. Introduce a helper to check if pricing data is "complete".
2. For "Today's" prices: only set `_date_prices_today_fetched = current_local_date` if the data is complete (covers the full 24h window).
3. For "Tomorrow's" prices: only stop polling if we have a full set of prices.
4. This ensures that if we get a "half-day" response, we continue to poll the API in subsequent cycles until we get a full day of prices.

---

## Skeptic (Senior SRE/Security)
Archi's plan is a good start, but it has flaws:

1. **API Spamming:** If the Pstryk API is intentionally serving partial data, retrying every 15-30 minutes indefinitely might lead to rate-limiting. We should ensure we don't retry faster than the standard poll interval.
2. **Fragile Completeness Check:** Comparing UTC timestamps directly can be tricky. We should check the number of frames (23-25 for a day) AND ensure the last frame end-time is at or after the window end.
3. **Storage Overhead:** Avoid excessive writes to `Store`. Only save if the new data is "better" (more frames) than the cached one.

---

## Consolidated Approach
1.  **Completeness Check**: A new function will verify if the frames cover at least 23 hours.
2.  **Conditional Success Flag**: `_date_prices_today_fetched` will only be updated if the data is complete.
3.  **Merge/Upgrade Cache**: If new partial data is received, it should only overwrite the cache if it contains *more* frames than the existing cache (or if it's a new day).
4.  **Logging**: Add debug logs for "Partial data received, will retry later".

**Skeptic's Final Approval:**
I approve this consolidated approach. It ensures data completeness without introducing recursive loops or excessive API load.

**Approved by Skeptic.**

---

## Update: Fix for UnboundLocalError
A bug was discovered where `_is_pricing_data_complete` was accessed before its definition.

**Archi:** We need to move all nested helper functions (`_has_meaningful_price_data`, `_is_pricing_data_complete`, `_are_frames_for_expected_date`) to the top of `async_update_data`.
**Skeptic:** 
1. Correct, this is a standard Python hoisting issue with local variables.
2. We should group them at the very beginning of the function to ensure they are available for all logic below.
3. This change is safe and straightforward.

**Approved by Skeptic.**

---

## Update 5: "Better Data" Strategy for Zero Prices
The user clarified that zeros can occur in partial data and might later change to real prices, or even be valid in the final data. A simple completeness check is insufficient.

**Archi:** 
1. I will move away from a strict "stop polling when complete" for tomorrow's prices.
2. Instead, I will implement a "Better Data" rule: the cache is updated if the new response has more frames OR the same number of frames but **more non-zero prices**.
3. For tomorrow's prices, we will continue polling even if we have 24 frames, as long as there are still 0.0 values that might be placeholders. If we reach 24 non-zero frames, we consider it "Ultimate Complete" and stop.

**Skeptic:** 
1. This is much more robust. It handles the "zeros first, then real prices" flow described by the user.
2. It's safe because it only overwrites the cache with "richer" data.
3. We should still avoid infinite polling for "Prosumer" prices if they are legitimately zero. I'll cap the retries or rely on the fact that at night prices are usually non-zero.

**Approved by Skeptic.**

---

## Update 7: "Any Change, No Regression" Strategy
The user indicated that my previous logic was still insufficient. The requirement is: update the cache if ANY value changed, but block "24-zero placeholders" if we already have non-zero data.

**Archi:** 
1. I will implement a helper `_should_accept_new_pricing_data(new, old)`.
2. **Criteria for accept**: 
   - `new` is NOT empty.
   - AND (`old` is empty OR `new` is not "all zeros" while `old` has prices).
   - AND (`new` has more frames than `old` OR `new` was different from `old`).
3. This ensures that even a tiny price change (e.g. 0.22 -> 0.23) is captured, while preventing a "wipeout" to all zeros.

**Skeptic:** 
1. This matches the user's request perfectly: "if at least one hour changed -> update".
2. The "placeholder protection" (blocking 24 zeros if we have real data) is the safety net.
3. We should ensure this applies to all four data points (Today/Tomorrow, Purchase/Prosumer).

**Approved by Skeptic.**

---

## Update 8: [2026-06-10 08:36] Задача: перестали обновляться цены продажи (проблема с кэшированием)

### Archi
Вчера всё работало, а сегодня цены продажи перестали обновляться и ушли в 'Неизвестно'. Проблема кроется в логике кэширования в файле `__init__.py`:
1. При смене суток кэш цен на сегодня (`_cached_purchase_prices_today` и `_cached_prosumer_prices_today`) не сбрасывается и не обновляется данными из вчерашнего кэша на завтра (`_cached_..._tomorrow`). В итоге в кэше "на сегодня" остаются вчерашние цены с вчерашними временными метками. Сенсоры не находят рамку на текущий час и показывают 'Неизвестно'.
2. Метод `_should_accept_new_pricing_data(new_data, old_data)` сравнивает новые цены на сегодня со старыми ценами в кэше (которые относятся к другому дню). Если сегодня цены все нулевые (например, ночью при отрицательных ценах или ограничениях), а вчера были ненулевые цены, срабатывает защита от регрессии к нулям, и новые цены отклоняются.
3. Переменная `_date_prices_today_fetched` обновляется только при успешной записи новых данных (`successfully_updated_any_today_prices = True`). Если данные уже были загружены из постоянного хранилища (кэша) при перезагрузке, этот флаг равен `False`, и `_date_prices_today_fetched` остаётся `None`, что приводит к постоянным фоновым запросам к API каждые 15 минут.

**Предлагаемое решение:**
1. При смене суток переносить завтрашний кэш в сегодняшний.
2. В функцию `_should_accept_new_pricing_data` добавить проверку даты. Если даты первой рамки в новых и старых данных не совпадают, безусловно принимать новые данные, так как старые устарели.
3. В конце обновления устанавливать `coordinator._date_prices_today_fetched = current_local_date` всегда, когда кэшированные данные для сегодняшнего дня полны и актуальны.

### Skeptic
Критика предложенного решения:
1. **Небезопасное извлечение даты**: При проверке дат через `parse_datetime` в `_should_accept_new_pricing_data` мы рискуем получить `AttributeError` или `IndexError`, если `new_data` или `old_data` придут с пустой структурой или без поля `start` в первой рамке. Необходимо делать безопасный разбор с валидацией типов и обработкой исключений.
2. **Часовые пояса при смене суток**: Сравнение дат `.date()` должно учитывать часовой пояс Home Assistant. Поскольку рамки API возвращаются в UTC (например, `2026-06-10T00:00:00Z` – это полночь UTC), в локальном времени Польши (CET/CEST) это может соответствовать 1:00 или 2:00 ночи. Метод `dt_util.as_local()` должен вызываться перед сравнением дат, иначе мы получим ложное несовпадение дат на границе суток.
3. **Гонка сохранений в хранилище**: Если мы переносим завтрашние цены в сегодняшние при смене суток в оперативной памяти, но HA перезагрузится до выполнения следующего API-запроса, изменения пропадут. Нам необходимо явно вызывать `store.async_save()` после переноса кэша или гарантировать сохранение нового состояния в конце каждой итерации `async_update_data`.

### Заключение
Решено внедрить следующие изменения:
1. Создать хелпер `_get_data_date(data)` для безопасного получения даты первой рамки с конвертацией в локальную таймзону HA.
2. В `_should_accept_new_pricing_data` использовать этот хелпер. Если даты в `new_data` и `old_data` различаются, принимать новые данные безусловно.
3. В `__init__.py` при смене даты переносить завтрашние кэшированные цены в сегодняшние:
   ```python
   if coordinator._date_prices_tomorrow_valid_for != tomorrow_local_date:
       if coordinator._cached_purchase_prices_tomorrow:
           coordinator._cached_purchase_prices_today = coordinator._cached_purchase_prices_tomorrow
       if coordinator._cached_prosumer_prices_tomorrow:
           coordinator._cached_prosumer_prices_today = coordinator._cached_prosumer_prices_tomorrow
       coordinator._cached_purchase_prices_tomorrow = {}
       coordinator._cached_prosumer_prices_tomorrow = {}
       coordinator._date_prices_tomorrow_valid_for = tomorrow_local_date
   ```
4. Корректно выставлять флаг `_date_prices_today_fetched = current_local_date` в конце `async_update_data`, если кэшированные цены для сегодняшнего дня полны и актуальны.
5. Изменения будут сохранены на диск в конце итерации обновления через существующий вызов `store.async_save()`.

---

## Update 9: [2026-06-10 08:43] Задача: Ошибка логики обновления кэша продажи (prosumer)

### Archi
В коде была допущена ошибка: в условиях вычисления флага `refresh_today_prosumer` отсутствовала проверка того, относятся ли находящиеся в кэше цены продажи к сегодняшнему дню. Условие выглядело так:
`refresh_today_prosumer = (coordinator._date_prices_today_fetched != current_local_date or not is_today_prosumer_complete)`
После успешного получения цен покупки флаг `_date_prices_today_fetched` выставлялся в `current_local_date`, из-за чего первое условие становилось `False`. А поскольку вчерашний кэш продаж был "полным" (содержал 24 рамки), второе условие тоже давало `False`. В итоге `refresh_today_prosumer` становился `False`, запрос к API блокировался, и интеграция продолжала использовать вчерашний кэш продаж с неактуальными временными метками.

**Решение:**
Добавить вычисление `is_prosumer_for_today` с помощью `_are_frames_for_expected_date` и дополнить условие:
```python
is_prosumer_for_today = _are_frames_for_expected_date(coordinator._cached_prosumer_prices_today, current_local_date)
refresh_today_prosumer = (coordinator._date_prices_today_fetched != current_local_date or not is_today_prosumer_complete or not is_prosumer_for_today)
```

### Skeptic
Замечания:
1. Данное изменение полностью логично и устраняет асимметрию между кодом обновления цен покупки и продажи.
2. Проверка даты кэша является критически важным триггером для сброса устаревших данных при смене суток.
3. Процедура безопасна и не вызывает лишних API-вызовов, если кэш уже актуален.

### Заключение
Решено дополнить условие `refresh_today_prosumer` проверкой актуальности даты кэша цен продажи (`is_prosumer_for_today`).

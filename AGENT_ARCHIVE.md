# 🗄 AGENT_ARCHIVE.md — хронология сессий Text Game RPG

> **Это архив, не инструкция.** «Читай первым» — `AGENT.md`; сюда заглядывают, чтобы понять,
> ПОЧЕМУ решение принято именно так, и не предложить повторно то, что уже пробовали/отвергли.
>
> Перенесено из `AGENT.md` в сессии 38 (пункт C8 аудита). **Текст сохранён дословно и не
> правится**: устаревшие пути (`app.py` вместо `routers/*`), цифры (`max_tokens=1600`,
> «44 пути», «правила 26–28») и номера строк оставлены как есть — они часть истории.
> Актуальная картина — в `AGENT.md` и по коду.

---

## 📊 ТЕКУЩЕЕ СОСТОЯНИЕ (проверять перед работой)

**Сессия 34 (пакет «реальность и устойчивость»: логи, бюджеты, перемотка, очередь, SSE, UX-фичи):**
- **Логи (E1, `logsetup.py`)**: JSON-лог в `data/logs/game.log` (ротация) + контекст хода через `turn_context(world_id, seq, agent)`; middleware добавляет `request_id`; `log_once()` для легальных фолбэков (не заливать лог), `reset_for_tests()`/`reconfigure()` для тестов и админки. Оставшиеся тихие `except: pass` → лог (правило 14), список и оправдания легальных фолбэков — в коде.
- **Перемотка (A1/A4/C1, `rewind.py`)**: единый `rewind_to(world_id, before_seq, mode="delete"|"hide", note)`; `folded` теперь 0/1/2 (visible/summary/hidden), `mark_folded` трогает только player/narrator; `load_save` переписан (раньше сворачивал ВСЁ подряд — терялась недавняя история); Chroma чистится по удалённым/сокрытым обменам и сводкам (`_purge_vectors`). Таблица `turn_snapshots(world_id, seq, setting, created_at)` — состояние ПЕРЕД ходом для отката (keep `TURN_SNAPSHOT_KEEP`). Эндпоинты: `GET /rewind/points`, `POST /rewind {seq, mode}`.
- **Бюджеты контекста (A2/B4)**: `world_recent_budget` = контекст − (измеренный промпт + ответ) − доля окна на память (было: догадка 2600 при реальном ~6200 и резерв 16384 > локальных 8192); `build_messages` возвращает `(messages, meta)` и **гарантирует невыход за окно** (усекает лор→RAG→карточки→recent, карточки сцены никогда). Ярусы промпта `gated_rules/trim_prompt` (−30% токенов в мире без подсистем; правила 9/9а/16 не выкладываются — риск задвоенного урона).
- **Очередь фоновых агентов (B3, `bg.py`)**: `submit(name, factory, priority, world_id, agent)` с heapq-приоритетом и семафором (`LLM_BG_CONCURRENCY`); `player_turn()` — ход игрока всегда первый; потолок очереди (`LLM_BG_MAX_QUEUE`); статистика в `/api/metrics` (`bg_queue`). Все 6 агентов переведены с `create_task` на `bg.submit` (deepcopy до лямбды!).
- **Ретраи (B2, `retry.py`)**: `with_retries(factory, config_key, ...)` + `is_transient()`; `llm.complete`/`stream_chat` (стрим повторяется только до первого токена), `chroma_client._raw`, `embeddings._http_json`. Ключи `LLM_RETRIES/LLM_RETRY_BACKOFF/LLM_TIMEOUT/CHROMA_RETRIES/EMBEDDING_RETRIES`.
- **Шина событий (D3, `bus.py`)**: `subscribe/publish/publish_threadsafe` + хук `db.add_event_listener` (события копятся в транзакции, рассылаются после COMMIT); `GET /api/worlds/{id}/events/stream?after=` (SSE + heartbeat), поллинг `/events?since=` остаётся запасным.
- **Метрики качества (E3)**: `llm._capture_finish` (finish_reason/usage); `metrics.record` пишет `cut_by_limit/cut_mid/prompt_trimmed/rag_score_avg/bg_queue`; `retrieve_memory(..., scores_out=[])` — оценки релевантности; `_memory_audit(..., rag_scores)` кладёт score в плашку «🧠 Память».
- **Игровые фичи (backend + фронт)**: `journal.py` (дневник: детерминированный дифф значимого, `record_turn` в транзакции хода, kind="journal" НЕ в контексте модели, `/journal` вкладка, `/journal note …`); `risk.py` (`/risk <идея>` — 9 осей ресурсов, чистый форматировщик без LLM/вердиктов); `chekhov_update/chekhov_text` (ружья Чехова: заряжаются из диффа, снимаются по упоминанию значимым словом, гаснут по TTL 12, «🏹 На горизонте» в format_state + правило 36); C5 (npc.notes → «🗝 Знания и тайны» в state + правило 35), C6 (`enemy_effect_add/remove`, `enemy_mark` — только хранение, не тикают; правила 34), C8 (`quest_success/quest_fail`, `quest {timer}` → дедлайн квеста через `setting.timers`; правила 33). Правила 33–36 в промпте; директивы зарегистрированы в `GAME_ENGINE_TOOL`/fmt_head/examples/`_EFFECT_KEYS`.
- **Выборки (B5)**: `get_unfolded_events/get_turn_events/count_events/get_latest_by_role/get_last_exchange/get_history_page/get_summary_events` + индексы `(world_id, folded, seq)`, `(world_id, role, seq)`; `/history` отдаёт страницу из SQL.
- **E2**: `ruff.toml` (E4/E7/E9/F, line-length 140, ignore E501/F401), `mypy.ini` (нестрогий), CI-шаги ruff (обязателен), mypy/coverage (continue-on-error). `.env.example` дополнен 15 ключами.
- **Фронтенд**: вкладка «Дневник» (журнал + заметка + фильтр), кнопки «⏪ Назад к ходу» и «🧭 Мои средства», компас соседей (`#compass-bar`), часы мира (`#world-clock`), EventSource-подписка (поллинг фолбэк).
- **Тесты**: 303 passed (было 278): `test_session34_rewind.py` (A1/A4/C1/B5/fold), `test_memory_layer.py` (A2/B4), `test_session34_features.py` (journal/risk/chekhov/rewind-endpoints).
- **Проверено живьём**: перезапуск на 8002; `GET /journal` после `/journal note` отдаёт запись; `/risk?idea=взлом` — справка; `/rewind/points` растёт с ходами; метрики `cut_by_limit_rate=0`; `data/logs/game.log` пишется с `[w<id>]`-контекстом.

**Сессия 35 (баг-хант сессии 34 — ВСЕ 5 багов исправлены):**
- **`_master_busy` не очищался (баг 1, критично)**: `_maybe_autonomous_master` держал `discard` ТОЛЬКО в `except` (finally: pass). Любой успешный/ранний выход (мастер выключен, интервал не накоплен, не застрял, сбой генерации) НАВСЕГДА блокировал автономного мастера до рестарта. Исправлено: `discard` перенесён в `finally` (как у `_enemy_ai_busy`/`_judge_busy`). Регрессия: `tests/test_session35_bugfixes.py::test_master_busy_released_on_early_return`.
- **Сводки «будущего» утекали в промпт при hide-перемотке/загрузке сохранения (баг 2, A4-дыра)**: `get_summary_events` НЕ фильтровал по `folded` → сокрытая сводка (folded=2) попадала в `_summaries()` → `build_messages`. Плюс stale-сводки с `seq < точки отката`, покрывающие откатываемый диапазон, не помечались в hide и не удалялись в delete. Исправлено: (а) `db.get_summary_events(..., unfolded_only=True)` по умолчанию отдаёт только folded=0 (rewind/анализ передают `unfolded_only=False`); (б) в hide-режиме stale-сводки с `seq < seq` помечаются `FOLD_HIDDEN` (по одной), в delete — удаляются через новый `db.delete_events_by_seq`; (в) `_purge_vectors` в hide чистит все stale. Регрессии: 4 теста (`test_summary_events_filters_hidden_by_default`, `test_rewind_hide_marks_stale_summary_below_rewind_point`, `test_rewind_delete_removes_stale_summary_below_rewind_point`, `test_rewind_hide_summary_after_point_hidden_and_filtered`).
- **Ключи дневника нестабильны между процессами (баг 3)**: `journal.record_turn`/`add_player_note` и `routers/worlds.py::/journal note` строили `entity_key` через `abs(hash(...))` — PYTHONHASHSEED рандомизирует hash между рестартами → дубликаты и сломанный дедуп. Исправлено: `journal._stable_key()` (hashlib.md5 от строки), применяется во всех трёх местах. Регрессии: `test_journal_key_stable_across_processes`, `test_journal_note_key_stable`.
- **Подсказка бюджета в UI устарела (баг 4)**: `updateGenUI` считал `ctx − 2600 − max_tokens − 16384` (старые константы до A2). Исправлено: `ctx − (2600 + max_tokens) − max(512, min(16384, floor(ctx×0.2)))` — та же формула, что серверный `world_recent_budget` для нового мира (измеренный оверхед недоступен на фронте до хода, поэтому 2600 как запас). Регрессия: `test_ui_budget_matches_server_formula` (UI == server для 32k/8k/256k/16k).
- **Дублированный мёртвый `except` в core.py (баг 5, косметика)**: два одинаковых `except Exception as e: log.warning("метрики хода…")` подряд — второй блок недостижим. Убран. Регрессия: `test_no_duplicate_except_metrics`.
- Проверено: **312 pytest** (303 + 9 новых), механика 4/4, `check_frontend.py` (id-ссылки 144/144), `node --check app.js`, сервер перезапущен на 8002.

**⚠️ Найденные баги сессии 34 — ВСЕ ИСПРАВЛЕНЫ в сессии 35 (см. блок «Сессия 35» выше).** Список ниже сохранён как история: 1) `_master_busy` не очищался; 2) сводки «будущего» утекали в промпт при hide-перемотке; 3) ключи дневника через `abs(hash)`; 4) устаревшая формула бюджета в UI; 5) дублированный `except` в core.py. Каждый закрыт регрессионным тестом в `tests/test_session35_bugfixes.py`.

**Сессия 36 (аудит ~21 000 строк: исправлено 21 найденный дефект):**
- **↻ после рестарта не задваивает ход (п.3B)**: события хода теперь носят метку `meta.turn`
  (= seq действия игрока) — её пишут narrator/dice/system-события ядра хода.
  `routers/core.py::_turn_registry` при промахе in-memory реестра собирает набор заменяемых id
  из БД по этой метке; для миров, записанных до неё, — консервативный фолбэк (нарратив/кубы в
  пределах нескольких seq, системки и 🌙-видения не трогаем). `invalidate_turn_registry()`
  вызывается после перемотки/загрузки слота, иначе ↻ оперся бы на удалённые id.
- **Барьер перегенерации (п.3A)**: сам барьер живёт в `bg.py` (`bg.regen_block(world_id)` →
  yield False при повторном захвате, `bg.is_regenerating(world_id)`), потому что о нём обязан
  помнить и слой памяти, а импорт core из memory создал бы цикл. `_bg_may_write(world, fresh,
  marker, last, base_turns)` в `routers/core.py` — единая проверка перед ЛЮБОЙ записью
  фонового агента: не идёт ли ↻ этого мира, не обновлён ли его маркер, и не сдвинулся ли
  `_player_turns` с момента чтения состояния. Прежняя проверка по одному лишь маркеру позволяла судье/мастеру/боевому ИИ/
  событию перезаписать setting состоянием на момент их чтения (п.17 — потеря хода игрока).
  Повторный ↻ того же мира = 409 (защита от двойного клика).
- **Механика не протекает в чат (п.4)**: `narrator.split_engine` переписан как ОДИН сканер по
  тексту: `_iter_json_objs` (балансирующий скобки, учитывает строки/экранирование) +
  `_is_engine_obj` (JSON похож на директивы: ≥1 известного ключа и не больше одного неизвестного)
  + `_cut_engine_blocks` (режет блок, маркер и закрывающую `)` вызова). Работает для `<<ENGINE>>`,
  `game_engine(...)` и «голого» блока в середине/конце; проза до и после неё остаётся у игрока.
  Ключи директив берутся из `mechanics.DIRECTIVE_CHAIN` (константа `ENGINE_KEYS` + `roll`/`dice`
  явно), поэтому список не разъезжается с движком. Мёртвые `ENGINE_RE`/`GAME_ENGINE_TEXT_RE`
  (greedy-захват) удалены. `engine_tail_hold(text)` — сколько символов стрима держать в буфере,
  чтобы игрок не увидел обрывок маркера; в старом стриме `safe` в ветке «маркера нет» не
  двигался → проза уходила дважды (закрыто `test_stream_marker_split_across_tokens`, сверенным
  с «старым» алгоритмом). `_finish_cut_reply` перед дописыванием срезает хвост механики.
- **Провидение (п.2, минимально безопасный фикс)**: `DIVINE_COOLDOWN_TURNS` по умолчанию 3
  (было 0 — бесконечный фарм `add_item/gold`); в промпт добавлено правило «Границы исправления»
  (чиним ровно заявленную потерю, без новых подарков/лечения/прокачки); `_divine_grants()` пишет
  каждую ресурсную выдачу в лог WARNING. 0 по-прежнему отключает лимит (админка/`.env`).
  ⚠ Живой smoke поймал ДЕФЕКТ этого же фикса: `setting.get("_divine_last_turn", 0) or 0`
  схлопывал «ещё ни разу не воззывался» с «воззывался на ходе 0» — в новом мире боги молчали
  первые cd ходов, ровно там, где жалоба на рассказчика нужнее всего. Теперь «не воззывался»
  (None) пропускается, кулдаун считается только от реального прошлого воззвания
  (регрессия `test_divine_first_call_never_blocked`).
- **КОРНЕВАЯ ПРИЧИНА, найденная при проверке п.2 (`frontend/admin.html`)**: новый дефолт
  `DIVINE_COOLDOWN_TURNS=3` на живой базе НЕ действовал, хотя кодом изменён. Причина: `load()`
  предзаполнял поля текущим **эффективным** значением, а `collect()` отправлял любое непустое →
  КАЖДОЕ сохранение админки «материализовало» все ~42 эффективных значения во временную
  таблицу переопределений (приоритет env → **админка** → .env), и ни один будущий дефолт
  .env/кода до них уже не доставал. Исправлено: при загрузке формы снимается `dataset.orig`
  для всех контролов, а `collect()` (хелперы `valOr`/`boolOr`) шлёт **только изменённые**
  поля; пустое поле по-прежнему = сброс к .env. Проверено живьём: сохранение «ничего не
  менял» не добавляет строк, а сброс `DIVINE_COOLDOWN_TURNS` возвращает дефолт 3.
  ⚠ На этой базе в admin_settings лежат строки, закреплённые прежними сохранениями, —
  чтобы дефолты кода заработали, нужен «Сбросить всё к .env» (он корректно удаляет строки).
- **Импорт мира (п.5)**: `db.restore_world` прогоняет setting через `ensure_player_schema` +
  `normalize_setting_ranks` и создаёт пустого player, если его нет; `_restore_world` пропускает
  битые события с `log.warning` (раньше одна мусорная строка роняла весь импорт откатом
  транзакции). Попутно закрыт дефект шире импорта: `ensure_player_schema` не гарантировал
  `hp/mp/gold/level/inventory`, а `apply_directives` в финале читает `p["hp"]` → неполный player
  ронял КАЖДЫЙ ход (теперь схема достраивает оси, а финальный чек смерти — через `.get()`).
- **Секреты (п.18, страховка правил 3/15)**: `db.world_dump` рекурсивно вырезает поля `api_key`
  из всей структуры, включая JSON-строки (setting/saves/turn_snapshots), с сохранением типов
  (`_dump_strip_secrets`/`_dump_has_secrets`). `llm._safe_body()` маскирует `sk-…`/`Bearer …` в
  теле ответа провайдера, которое попадает в текст ошибки (и далее в лог/UI).
- **Горячие пути (п.8/9/10/12)**: `db.list_entities(world_id, kind, limit)`; `select_relevant_entities`
  и `update_entity_cards` читают окно `_SCENE_CARDS_WINDOW = 400` (было: 1 + 4 полных выборки всех
  карточек на каждый ход; в update_entity_cards — одна выборка + `_merge_cards` вместо повторных
  SELECT). ⚠ Попутно закрыт РЕАЛЬНЫЙ дефект: три блока синхронизации (квесты / NPC /
  магазины-враги-спутники-рецепты) парсили ОДНУ И ТУ ЖЕ строку `world["setting"]` и каждый в
  конце писал `update_world(setting=...)` целиком — **поздний блок затирая правки раннего**
  (из трёх синхронизированных секций в БД доходила последняя: квест и NPC молча исчезали).
  Теперь setting парсится один раз, правки копятся в одном объекте и пишутся ОДНОЙ записью;
  блок также перечитывает свежий `world` и пропускается под барьером ↻
  (регрессии: `test_cards_sync_persists_all_sections`, `test_cards_sync_skipped_during_regen`);
  `narrator.feedback_style_note` — хвост `_FEEDBACK_SCAN_LIMIT = 60`; новый `db.get_events_after`
  (первые N по seq вперёд — `get_events(limit=)` режет ХВОСТ, для пагинации не годился), и
  `_reindex_imported_world` идёт страницами по 500; `db._list_worlds` считает ходы одним
  `LEFT JOIN … GROUP BY` вместо correlated COUNT(*) на строку.
- **Журнал метрик (п.7/24)**: `_rotate_journal` (`METRICS_MAX_BYTES`, по умолчанию 4 МБ × 3 архива —
  раньше METRICS_TAIL ограничивал только чтение); `_ensure_totals_baseline()` снимает кумулятивы
  из журнала ОДИН раз, ДО первой собственной записи (иначе свои ходы попали бы в счёт дважды),
  отчёт отдаёт `counters.source = process | process+journal` + `journal_baseline`.
- **Блокировки и жизненный цикл (п.11/30)**: `db._run` — потолок ожидания `_RUN_TIMEOUT = 120с`
  (ошибка + `log.error`, а не вечный стоп) и явный `RuntimeError` при вызове из потока
  `aiosqlite-loop` (само-дедлок); `world_dump`/`export_data` читают таблицы под `_lock` (иначе
  между запросами вклинится чужая транзакция хода → дамп «наполовину из хода»). В `app.py`
  — `lifespan` (вместо `on_event`): при остановке закрываются httpx-клиенты (chroma/embeddings/llm),
  воркеры `bg.shutdown_nowait()` и цикл БД (`asyncio.to_thread(db.close)`) — на Windows файл
  game.db больше не залочен после штатного выключения.
- **Прочее**: `NameError: log` в `routers/entities.py` (п.1 — 500 вместо «карточка удалена» при
  сбое чистки Chroma); `hybrid_rerank` клампит вес BM25 в [0,1] + 400 в админке на невалидное
  значение (п.29); коррекция уровня судьёй больше НЕ лечит бесплатно — пропорция HP/MP
  сохраняется, мёртвый остаётся мёртвым (п.26); Edge-TTS повторяется через `with_retries` +
  `transient_fn=_edge_transient` (свои исключения edge_tts не считались временными) (п.28);
  `admin_settings.read_overrides` — одноразовый WARNING при недоступной таблице (п.23);
  `config.strip_env_comment` срезает хвостовые `#`-комментарии `.env` вне кавычек (п.13);
  `start_game.bat` считает llama.cpp живым при 401/404 (проверка HTTP-кода, а не errorlevel curl)
  (п.14); per-world провайдеры при сохранении проверяют доступность модели и возвращают
  `warnings` (не блокировка) (п.19); фронт: `jsAttr()`/`numAttr()` для ключей внутри `onclick`
  (апостроф в id от LLM ломал модалку) (п.25), быстрые действия при пустом ответе сервера ВСЕГДА
  просят свежие, с троттлингом `_SUG_MIN_INTERVAL_MS` (п.15), `pollTtsStatus` хранит таймер на
  кнопке (п.16); эвристики `_looks_finished`/`_repetition_score`/`_dedupe_repeats`
  задокументированы как инварианты с известными компромиссами (п.31), а `_dedupe_repeats`
  перестал склеивать абзацы в простыню (сохраняет разделители). Пункты 20/22/27 аудита —
  verify-only: проверены и правок не потребовали.
- **Тесты**: **387 pytest** (312 + `tests/test_session36_bugfixes.py` — 75 новых, по одному-двум
  на каждый пункт). В `scripts/check_frontend.py` добавлены проверки: подстановки в `onclick`
  обязаны идти через `jsAttr`/`numAttr` (+ roundtrip грязных ключей в node с анти-проверкой
  «старое esc обязательно ломается»), `cjk_check` (иероглифические вставки в кириллических
  комментариях — правились в `tests/conftest.py` и `start_game.bat`) и `omoglyph_check`
  (латинские омоглифы внутри кириллических слов: в HEAD нашлось 5 — «вcё», «копятcя»,  <!-- TYPO-OK: цитата исправленной опечатки -->
  «фаcаd», «пересh», «меx», все исправлены; доказательность — прогон сканера по файлам из  <!-- TYPO-OK: цитата исправленной опечатки -->
  HEAD, в вычищенном дереве 0 ложных срабатываний).
- Проверено: 387 pytest, механика 4/4, `scripts/smoke_session36.py` на живом сервере, `check_frontend.py` (id 144/144 + jsAttr roundtrip),
  `node --check app.js`, `check_plot.py` 3/3, синтаксис 59 файлов, сервер перезапущен на 8002.

**Сессия 37 (git-гигиена: довести CI-шаг `ruff check backend` до нуля + закрыть F821):**
- **F821 → живой `NameError` в `mechanics.py` (критично)**: в шапке модуля не было `import json`,
  а `NpcHandler` при `npc_set {notes:{…}}` с не-строковым значением (`["мех","щит"]`, число,
  вложенный dict) звал `json.dumps(...)` → `NameError` в фоне (директива из цепочки, изолирована
  try/except, но заметка терялась молча). Фикс: `import json`. Регрессия:
  `tests/test_mechanics.py::test_npc_notes_non_scalar_values` (падает и при откате импорта — NameError).
- **Попутно вскрыт второй, более тихий дефект C5 (тот же блок, родом из сессии 34)**: прежние
  `notes` читались для слияния ПОСЛЕ `existing.update(...)`, которое уже перезаписало их новыми →
  «MERGE» терял все ключи мастера, заведённые ранее (второй `npc_set {notes:{хочет:…}}` стирал
  `тайна/долг`). Фикс: прежние `notes` снимаются в `_prev_notes` ДО bulk-update. Та же регрессия
  проверяет накопление ключей и падает при откате любого из двух фиксов (анти-тест прогнан).
- **ruff `check backend` → 0** (было 34 в рабочем дереве, 36 на HEAD). Разбор: 6 автофиксов
  (`--fix`: F541/E703), 7 мёртвых локалей (F841: `cfg`/`text`/`p`/`meta`/`vis_ev`/`persona`), 3×E741
  (`l`→`_loc`/`_ln`), 10×E701 (`if x: L.append(…)` перенесены в теле), 8×E402 — НАМЕРЕННЫЕ
  (фасад/реэкспорты во избежание циклов импортов) → заглушены точечно в `ruff.toml
  [lint.per-file-ignores]` (только narrator/embeddings/routers{worlds,lore,system}), а НЕ глобально
  и НЕ `continue-on-error` в ci.yml (легализация красного линтера отвергнута).
  ⚠ В `suggest_actions` удалён мёртвый `persona = _world_persona(world)`: `generate_suggestions`
  персону не принимает — если понадобится «подсказки в голосе NPC», это отдельная доработка
  с параметром в narrator (не молча «оживить» переменную).
- Проверено: **388 pytest** (387 + `test_npc_notes_non_scalar_values`), `ruff check backend` — All checks
  passed, AST 60 файлов, `import backend.app` OK, механика 4/4, `check_frontend.py` ✅ (0 CJK/омоглифов),
  `check_plot.py` 3/3, `node --check app.js` OK, секретов в diff 0, `.env.example` без ключей; сервер
  перезапущен на 8002 (новый код). ⚠ Вне CI-scope (`check backend`) в `tests/`/`scripts/` ruff находит
  ещё 17 (E741/F841) — на зелёный CI не влияют, чистятся по желанию отдельным коммитом.
- **CI-грабли (важно на будущее)**: первый пуш (`8be0665`) CI повалил красным — три edge-TTS теста
  падали с `ModuleNotFoundError: edge_tts`: CI ставит только `requirements.txt`, а edge-tts живёт в
  `requirements-optional.txt`. Исправлено (`7ff08cf`): `pytest.importorskip("edge_tts")` в трёх тестах.
  ⚠ Правило: любой тест на ОПЦИОНАЛЬНОЙ зависимости (edge-tts / sherpa-onnx / fastembed) обязан
  защищаться `importorskip` — локально всё стоит и падений не видно, краснота вспыхивает только в CI.
  Run #3 (`7ff08cf`) — **CI зелёный**; в нём 3 пропуска edge-тестов (на машине владельца, где edge_tts
  стоит, все 388 исполняются).
- **Git**: решён План Б (владелец) — один коммит сессий 35+36+37: `8be0665` (37 файлов), CI-фикс
  `7ff08cf`; запушено в `origin/main` 2026-09-01. ⚠ Правки 35 и 36 физически в одних файлах и одних
  хунках — дальнейшие попытки «делить по сессиям» постфактум бессмысленны. Живая `admin_settings`
  (41 материализованная строка) решена владельцем: НЕ сбрасывать, оставить как есть — дефолты .env
  её перебить не могут (приоритет: env → админка → .env); в git из конфигов попадает только
  `.env.example`.

**⚠️ Подводные камни сессии 36 (проверять при доработках):**
1. `meta.turn` — признак СОБЫТИЙ ХОДА. Новый агент/директива, пишущие события в ход, обязаны
   ставить метку, иначе ↻ их не заменит. Фоновые события мира метку НЕ носят.
2. Фоновый агент пишет setting ТОЛЬКО через `_bg_may_write(...)` (ядро) или проверку
   `bg.is_regenerating()` (память) сразу после перечитывания — await между чтением и записью
   = гонка. Не возвращаться к `s`/`world`, снятым в начале функции.
2а. Любая синхронизация «карточки → setting» обязана править ОДИН распарсенный объект и
   писать его один раз в конце. Парсить `world["setting"]` по месту в каждом блоке нельзя:
   следующий блок затрёт предыдущего (проверено на старом коде — терялись квесты и NPC).
3. Новый формат вывода модели в `split_engine` добавляется в `_iter_json_objs`/`_is_engine_obj`,
   а не отдельным регэкспом. `ENGINE_KEYS` пополняется из `DIRECTIVE_CHAIN` сам (`roll` — явно).
4. `ensure_player_schema` — единственная гарантия числовых осей игрока; `apply_directives` больше
   не падает на неполном player, но молча чинить кривые данные вне схемы нельзя.
5. `db._run` бросает RuntimeError из потока цикла БД: фоновым корутинам на этом цикле нельзя
   вызывать синхронный API db.* (выполняй корутины `_*` напрямую или иди через bg-очередь).
6. `db.get_events_after` — вперёд по seq; `get_events(limit=N)` — ХВОСТ из N последних. Не путать.
7. `_totals_baseline` обязан сниматься ДО первой `_persist` этого процесса (иначе двойной счёт).
8. `Config.load(env=…)` подменяет falsy-значение на `os.environ`: в тестах передавай
   `env={"__NONE__": "1"}`, а не `env={}`.
9. `.gitignore` кроет `.env.*` — временный бэкап `.env.bak.session36` в git не попадёт.
10. **Админка шлёт только изменённые поля** (см. блок «Сессия 36» про `dataset.orig`):
    новое числовое/булево поле формы ОБЯЗАНО заполняться через `setAg`/`setAgCheck` или
    ставить `el.dataset.orig` явно, иначе `valOr` сочтёт его «не меняли» и правка не сохранится.
    Любое изменение дефолта в `.env`/коде на живых базах перебивается ранее материализованной
    строкой в admin_settings — при подозрении «дефолт не применяется» смотреть её.
11. `check_frontend.py` глушит строку маркером `TYPO-OK` (как noqa): он нужен
    там, где документация ЦИТИРУЕТ исправленную опечатку/иероглиф, — иначе док о
    фиксе не проходил бы сам фикс. Не удалять при «зачистке текста».

**Подводные камни сессии 34 (проверять при доработках):**
1. `admin_settings` читает БД отдельным соединением — config→db→config вешает сервер. LOG_* резолвятся в logsetup напрямую из env/.env (без админки).
2. rewind в "hide" разворачивает строго `[unfold_from; seq-1]` — иначе вернёт сокрытое будущее.
3. `bg.submit` принимает и лямбду, и корутину (`_CoroutineAdapter`); deepcopy — ДО лямбды.
4. `llm.stream_chat` повторяется только до первого переданного токена.
5. `build_messages` теперь возвращает `(messages, meta)` — обновляй вызывающих.
6. `mark_folded` по умолчанию трогает только player/narrator.
7. journal-карточки не должны попадать в `select_relevant_entities` (фильтр, а не штраф).
8. `db.add_event_listener` копит события в транзакции, рассылает после COMMIT (не на rollback).
9. Правила 9/9а/16 не выкладываются ярусами промпта (риск задвоенного урона).
10. `lore_rules` — НЕ f-строка: фигурные скобки одинарные; в f-строках fmt_head/примеров — двойные.
11. В тестах `LOG_FILE` = temp; `root.propagate=True` + NullHandler на корне (иначе caplog слепнет).

**Сессия 33 (git + безопасность + переносимость миров + тесты слоя памяти):**
- **Git-репозиторий** `origin = https://github.com/pL1uXa-AI/text_game_rpg` (приватный): `.gitignore`
  (секретные `.env`/`.env.*`, вся папка `data/` = 841 МБ, логи, `nul`, кэши, черновики, папка заметок
  `НЕ УДАЛЯТЬ ПАПКУ. ИДЕИ/`), `.gitattributes` (LF в репо, CRLF для `.bat`), `.env.example` (шаблон без
  ключей), `requirements.txt` + `requirements-optional.txt` (импорты TTS/fastembed ленивые — без них игра
  работает), `scripts/setup_env.bat` (окружение с нуля: .env → pip → свой venv ChromaDB).
- **Безопасность**: `start_game.bat` слушает **127.0.0.1** (было `--host 0.0.0.0` при живых ключах и
  отсутствии авторизации; bind переопределяется `GAME_BIND`). `_mask_provider` не менялся, но дамп мира
  обязан быть без ключей — см. правило 15.
- **Переносимость**: `db.world_dump(id)` → JSON (setting + ВСЕ события + entities + lore + saves +
  graph_nodes/edges + gen/provider/tts/snapshot/narrator_name, `api_key` вырезаны); `db.restore_world(dump)` →
  **новый** мир (рассказчик ищется по имени, события/карточки/лор/слоты/граф переносятся, id не затираются);
  `db.backup_database(keep)` → согласованный снимок game.db (SQLite Backup API) в `data/backups/` с ротацией,
  вызывается при старте из `app.py` (`BACKUP_DB_ON_START`). API: `GET /api/worlds/{id}/export/json
  [?download=1]`, `POST /api/worlds/import/json` (`schemas.ImportIn`) + фоновый
  `routers/worlds.py::_reindex_imported_world` (лор → карточки → обмены парами «действие → ответ»).
  UI: «🗄 Полный дамп (JSON)» и «📥 Импорт из дампа» (Настройки мира → «Действия»). Content-Disposition —
  только ASCII-фолбэк + `filename*=UTF-8''` (русское имя мира иначе роняет ответ: заголовки latin-1).
- **Авто-детект контекста модели**: `llm.probe_context(provider)` — llama.cpp `/props`
  (`default_generation_settings.n_ctx`), Ollama `/api/show` (`*.context_length`), OpenAI-совместимые —
  `context_length`/`max_context_tokens`/… у нужной записи `/models` (или единственной); кэш по
  (base_url, model), `None` при недоступности. `routers/core.py::_context_guard(world_id, providers,
  gen_settings)` снижает `context_tokens` до 95% лимита и кладёт `context_limit`/`context_limit_source`
  в `gen_settings`; вызывается при создании мира и в `POST /settings`; при неизвестном лимите НЕ трогает.
  UI: строка «Реальный лимит модели…» в настройках + потолок ползунка (`#ctx-limit-note`). `DETECT_MODEL_CONTEXT`.
- **Метрики на диске**: `metrics._persist()` пишет каждый ход в `data/metrics.jsonl`, `read_journal()`
  читает хвост (битые строки пропускаются), `as_json()` при пустом in-memory буфере возвращает
  `restored_from_journal: true` — статистика больше не обнуляется рестартом. `METRICS_PERSIST/FILE/TAIL`.
- **Правило 14 продолжено**: `memory.retrieve_memory` молча глотал ЛЮБУЮ ошибку (отказ Chroma ≠
  «эмбеддинги выключены») → теперь `log.warning` с world_id; легальная пустая выдача при выключенных
  эмбеддингах осталась тихой и закрепления тестом.
- **Фикс админки (регресс-тест)**: «Сбросить всё к .env» писал `false` во все булевы тумблеры — после
  «сброса» были **выключены** судья логики, автономный мастер, боевой ИИ, события, озвучка. Теперь админка
  принимает список `reset: ["LOGIC_JUDGE_ENABLED", …]` (пустое значение = удалить строку из `admin_settings`
  → приоритет возвращается к `.env`), а UI шлёт `ALL_ADMIN_KEYS` и сохраняет выбранный провайдер.
- **Тесты** (278 total): `test_memory_layer.py` (41 — BM25/гибрид, порог косинуса cloud/local,
  коллекции по размерности, `retrieve_memory` всё: отбор/K/деградация/лог, схема id индексации,
  сворачивание только при успешной сводке, чанкинг лора, `_clean_tts_text`/`_split_chunks`/`_hash`/
  `_norm_rate`/`tts_effective`/`piper_voice_ready`), `test_world_dump.py` (11), `test_session33.py` (22).
  Общие фикстуры: `fake_config` в `conftest.py` (подменяет кэшированный Config, наследуясь от реального
  класса, monkeypatch возвращает старый кэш); в conftest же `BACKUP_DB_ON_START/METRICS_PERSIST/
  DETECT_MODEL_CONTEXT=false`, иначе pytest писал бы в боевой `data/`. Async-проверки гоняются через
  `asyncio.run` (pytest-asyncio в проект не добавляли).
- **Проверено живьём**: сервер перезапущен на 127.0.0.1:8002; `/openapi.json` 49 путей (дамп/импорт на месте);
  дамп мира 80 → импорт → мир 89 (4 события / 18 карточек / 23 лор-статьи) → cleanup; `sk-` в дампе — 0;
  `data/backups/game-*.db` создаётся при старте; метрика хода упала в `data/metrics.jsonl`;
  probe на облаке вернул 1 048 576 → мир с 262k оставлен без снижения; `check_plot.py` 3/3,
  `test_directives.py` 4/4, `node --check` app.js и скрипта админки.

**Сессия 32 (мега-пакет универсальных механик — идеи 1–9 из PROPOSALS; с сессии 33 этот файл слит в ROADMAP.md и удалён):**
- **⏳ Таймеры мира** (`setting.timers` + `timer_add {name, turns, desc}`/`timer_remove`): дедлайны (бомба/осада/прибытие). `mechanics.tick_world_timers` тикает в начале хода (core), по истечении — системное «⏰ Таймер истёк». Движок лишь напоминает — исход решает мастер (правило 30).
- **⚔️ Экипировка и слоты**: у предметов `slot` (голова/торс/руки/ноги/оружие/вторая рука/аксессуар) + `bonus {стат: +N}`; `equip/unequip` надевают/снимают (`player.equipped {слот: имя}`); код проверяет наличие/слот (нельзя два шлема) — «судья возможностей»; бонусы входят в `effective_stats` (и `equipped_bonuses`). UI: «🛡 Экипировано» + бонусы в статах.
- **🍖 Потребности + 🧠 рассудок**: `player.needs` (голод/жажда/усталость) и `player.mental` (рассудок/стресс/мораль) — шкалы `{value, max, decay}`; `tick_needs_mental` тикает в начале хода (`TICK_NEEDS_ENABLED`, .env/админка), предупреждает при критике («⚠️ голод на критическом уровне»); последствия ведёт мастер директивами (закон 3, правило 31). Директива `needs {голод: {value: -10}}`.
- **📅 Календарь/сезоны**: `setting.date {day, month, season}` + `date`-директива; сезоны дают моды в `environment_mods` (зима/весна/лето/осень).
- **⛰ Локации-зоны**: `location_add {..., effects: [...]}` — при входе эффект накладывается (`apply_location_effects`), при выходе снимается (core в move); «🌫 Влияние места» в состоянии/UI.
- **📜 Доска объявлений**: `setting.board []` + `board_add {title, text}` + `/board` + блок «Доска объявлений» в UI (закон 2 — отображение).
- **🏅 Звания во фракциях**: `player.faction_ranks {фракция: звание}` + `faction_rank {faction, rank}`; репутация = доверие, звание = должность (показ в UI/состоянии).
- **🌙 Сны/видения**: `setting.pending_visions []` + `vision_add {text, hint}` (в очередь); `POST /api/worlds/{id}/vision/trigger` разыгрывает отдельным LLM-проходом (`narrator.generate_vision`/`vision_messages`) — память как сюжет (сон/галлюцинация/сигнал).
- **🎲 Ставки на бросок**: `roll.stakes {success, fail}` — показываются в сообщении кубов.
- Механика: новые `DirectiveHandler` (Timer/Equip/Needs/Board/FactionRank/Calendar/Vision) в `DIRECTIVE_CHAIN`; `ensure_player_schema` добавил needs/mental/faction_ranks/equipped; `format_state` показывает всё новое; `GAME_ENGINE_TOOL`/промпт-правила 30–32. Тесты: `tests/test_mechanics_s32.py` (20) — итого **206 pytest**, механика 4/4, node --check, сервер перезапущен на 8002.
- **Ревизия (детальная проверка)**: `generate_vision` — исправлен `narrator.find_engine_start` → `find_engine_start` (NameError); `needs {value: -30}` — теперь дельта (не абсолют; абсолют — `{set: N}`); `TimerHandler` — `turns: 0` больше не становится бессрочным; `board_text` защищён от board-не-списка (+ восстановлен потерянный `board.append`); `location_add/update` сохраняют `effects` (зоны); move-зоны в core — `sys_msgs.extend` вместо затирания; `select_relevant_entities` — активные квесты приоритетнее знаний (+4.0) и стемминг упоминаний («гримуаром»→«гримуар») — стабилизирован флаки-интеграционный тест; очередь видений показана в `format_state` и UI; видение подмешивает RAG-факты (memories).

**Сессия 31 (память от контекста, все параметры в Админке/Настройках, устойчивость ответа и быстрых действий):**
- **Динамическая память от размера контекста** (жалоба «рассказчик всегда вспоминает 9 фактов, вне зависимости от контекста 262к»): новые `narrator.dynamic_memory_k(world, base, max)` / `narrator.dynamic_lore_budget(world, base, max)` масштабируют количество RAG-фактов/лор-чанков/сводок пропорционально `context_tokens` мира (база при 32k → больше при 128k/256k, потолок из конфига). Применяются в `retrieve_memory` (K), `retrieve_lore` (бюджет + K) и `build_messages` (сводки: 3 → до 10 при большом окне). Per-world `gen_settings.rag_memory_k / lore_rag_k / lore_token_budget` (0 = авто) переопределяют базу.
- **Все параметры в Админке** (`/api/admin/settings` + admin.html): новые секции «🧠 Память» (RAG_MEMORY_K/MAX, RAG_CANDIDATES, RECENT_TOKEN_BUDGET, SUMMARY_TOKEN_BUDGET, LORE_TOKEN_BUDGET/MAX, LORE_RAG_K/MAX, COSINE_THRESHOLD(+LOCAL)), «🎛 Фоновые агенты и мир» (LOGIC_JUDGE_ENABLED/INTERVAL, DYNAMIC_EVENTS_ENABLED/EVENT_EVERY_TURNS, AUTONOMOUS_MASTER_ENABLED/INTERVAL, ENEMY_AI_ENABLED/INTERVAL, TICK_EFFECTS_ENABLED, DIVINE_COOLDOWN_TURNS, MAX_ACTION_CHARS), реранкер top_n/threshold, TTS-кэш (TTS_CACHE_ENABLED/TTL/MAX_CHARS).
- **Настройки мира** (index.html + app.js): ползунки «Сколько фактов памяти вспоминать (RAG)» и «Лор-чанков из RAG» (0 = авто; потолки из `memory_defaults` от /api/worlds/{id}); сохраняются в gen_settings и работают через dynamic_memory_k.
- **Защита от обрывов/дублей ответа** (жалоба «текст обрезался, местами дублировался»): `routers/core.py::_dedupe_repeats` убирает подряд идущие точные дубликаты абзацев/предложений (≥30 символов); `_looks_finished` детектит обрыв на полуслове (модель в tools-режиме вызвала game_engine в середине ответа и бросила писать) → `_finish_cut_reply` дописывает 1–3 завершающих предложения отдельным LLM-проходом (при сбое — исходный текст, ход не роняется).
- **Быстрые действия не сбрасываются** (жалоба «быстрые действия сбросились на втором ответе»): `handleActionResult` больше НЕ очищает `state.suggestions` пустым массивом при сбое/таймауте генератора — при пустом результате оставляет старые ИИ-предложения и асинхронно запрашивает свежие (`loadSuggestionRefresh`). `generate_suggestions` получил 3-й фолбэк-парсер (короткие строки без разметки) — устойчивее к невалидному JSON модели.
- Тесты: **186 pytest** (+4 в test_session30: dynamic_memory_k_scales_with_context, dynamic_lore_budget_caps, dedupe_repeats_removes_adjacent_duplicates, looks_finished) + механика 4/4 + node --check app.js/admin.html + живой smoke (мир → ход → suggestions не пустые → memory/search 6 → dynamic RAG k=24 при ctx 262k). Сервер перезапущен на 8002.

**Сессия 30 (технический спринт из анализа: декомпозиция narrator.py, интеграционные тесты, метрики/трассинг, TTS-предзагрузка, DnD, rate-limit, LRU-кэш эмбеддингов):**
- **Декомпозиция `narrator.py`** (2950 → ~1830 строк, тонкий фасад): перенесены целые блоки (verbatim, семантика не менялась) в три новых модуля:
  - `backend/memory.py` — 🧠 память: `retrieve_memory`/`index_exchange`/`index_summary`, `summarize_and_compress`/`_make_summary`/`world_main_provider`, `select_relevant_entities`/`update_entity_cards`/`index_entities`, `ensure_knowledge_cards`+`_origin_for`/`_upsert_knowledge`, `compact_entity`/`format_entity_cards`;
  - `backend/lore_retriever.py` — 📖 лор: `seed_lore_from_theme/custom`, `_chunk_lore`, `index_lore_entry`/`index_all_lore`, `retrieve_lore`;
  - `backend/character_generator.py` — 🧙 персонаж: `generate_identity`, `generate_character`, `apply_character`, `generate_opening`, `_is_modern_world`/`_cut_words`/`_STAT_LU`.
  В конце narrator.py — **реэкспорт** (`from .memory import ...` и т.д.): все внешние вызовы `narrator.retrieve_memory(...)`, `narrator.apply_character(...)` и т.п. работают без изменений (фасад). Проверено тестом `test_decomposition_facade`.
- **Интеграционный тест** `tests/test_integration.py` (3 теста): герметичный сквозной сценарий «создать мир → 10 ходов с директивами → состояние/история/карточки знаний/select_relevant_entities» (включает реальную `ensure_knowledge_cards` через monkeypatch поверх conftest-noop) + лор-контур (сидинг→чанкинг→фолбэк retrieve_lore). ChromaDB в тестах НЕ поднимается (conftest заглушает) — реальный E2E с живым Chroma остаётся ручным (см. как тестировать).
- **Метрики/трассинг фоновых агентов**: `metrics.record_agent(agent, ms, world_id, ok)` — новый кольцевой буфер спанов; `routers/core.py` замеряет время судьи (`judge`), мастера (`master`), боевого ИИ (`enemy_ai`), динамических событий (`event`), карточек (`cards`), памяти (`memory`) и пишет в `GET /api/metrics` → блок `agents` (среднее/сумма/счётчики/ошибки + последние спаны). Проверено вживую: после хода видны judge/master/event/enemy_ai со временем.
- **TTS-предзагрузка голосов**: `tts.preload_voice`/`tts.preload_configured_voices` — при старте сервера прогревает sherpa-onnx для Piper/Kokoro (Edge — облако, не требует), чтобы первый ответ озвучился без задержки на инициализацию. Вызывается в `app.py` при импорте (best-effort).
- **Rate-limit без зависимостей**: новый `backend/ratelimit.py` — in-memory sliding-window + burst-корзина (`allow`, `make_guard` для FastAPI Depends, `configure(enabled)` для тестов). Применён к `POST /api/worlds/{id}/action`, `.../action/stream`, `.../divine` (defaults 120/60с, burst 30/5с — защита от залипания кнопки/цикла, легитимная игра не блокируется). В тестах выключен (conftest `configure(enabled=False)` — TestClient гоняет десятки действий с одного IP).
- **LRU-кэш эмбеддингов** (`embeddings.py`): `_emb_cache` (OrderedDict, 512, ключ provider|model|prefix+текст) — повторные запросы (общие фразы, повторная индексация карточек) не пересчитывают вектор; кэшируются и облачные, и локальные (fastembed) векторы. Тест `test_embeddings_lru_cache_hits`.
- **Drag-and-Drop для сохранений** (frontend): слоты `.save-item` перетаскиваются на зону-корзину «🗑 Перетащи сюда» (`bindSaveDnD`) → удаление с подтверждением; визуал `.save-trash-zone`/`.dragging` в style.css. Без backend-изменений.
- Не сделано (обоснование в анализе): Silero TTS (противоречит стандарту проекта: piper/kokoro/edge уже покрывают, torch ~2.5GB и не согласовано), WebSocket вместо SSE (нужны сервер+клиент+auth, SSE-поллинг уже работает), строгая валидация директив extra=allow (директивы — это JSON от LLM, а не пользовательский ввод; терпимость намеренная), Prometheus /metrics (новая зависимость prometheus_client для локальной игры не оправдана; метрики уже есть в /api/metrics).
- **Фикс карточек квестов (ревизия сессии 30)**: квестовые карточки знаний копили `desc` в `bio` каждый ход (простыня из повторов) и показывали латинский id вместо title. Исправлено: дедупликация `bio_add` в `db._upsert_entity` + `ensure_knowledge_cards` для квестов (`name=title`, `summary=desc`, `bio_add=None`). Регресс-тест + живая проверка.
- Тесты: **181 pytest** (175 старых + test_integration 3 + test_session30 3: rate-limit, трассинг агентов, LRU-кэш) + механика 4/4 + node --check + живой smoke (мир → ход → memory/search 6 результатов → lore 4 → карточки 16 → metrics.agents). Сервер перезапущен на 8002.

**Сессия 29 (сюжеты-миры → файлы в `plots/`, схемы PLOTS.md):** стандартные сюжеты (бывшие 36 THEMES) убраны из хардкода `narrator_data.py::THEMES` (там теперь пустой список) и живут **файлами** в `plots/system/` (комплектные) и `plots/user/` (свои), каждый — чистый JSON по полной схеме `PLOTS.md` с расширением `.js` (пример — `plots/system/pepel-kontrakta.js`, бывший «тестовый сюжет 3.txt»; корневой txt удалён). Новый **`backend/plots.py`**: сканирует папки при старте и по кнопке «🔄 Обновить сюжеты» (`POST /api/plots/reload`), строит тему-обёртку (`narrator.THEMES` — тот же список, `narrator.get_theme`), `GET /api/themes` сам проверяет mtime (`ensure_fresh`) — новые/правленые файлы подтягиваются без рестарта. `id` = поле `id` или транслитерация имени файла (кириллица → латиница корректно); `user/` переопределяет `system/`. **При создании мира из сюжета** (`worlds.py`): применяются лор (`lore_articles`→`seed_lore_from_theme`), `story.opening` как завязка, и `narrator.apply_plot_start(setting, plot)` — стартовое состояние (золото/инвентарь/локации/NPC/магазины/фракции/квесты/флаги/время/погода; структура полей совпадает с директивами движка; `stations`/`connections` принимают и список, и строку с запятыми; числа защищены от мусора). Роль игрока (раса/класс/статы) генерит `generate_character`, сюжет её НЕ задаёт. `apply_plot_start` вызывается **вне** try-блока персонажа — старт применяется даже при сбое генерации. **Миры не зависят от файлов**: в `setting["_theme_snapshot"]` копируется компактный снапшот темы при создании, `narrator._world_theme(world, setting)` использует его фолбэком, если файл позже удалён/правлен; для очень старых миров без снапшота синтезирует минимальную тему из полей мира (не падает). UI «Новый мир»: кнопка «🔄 Обновить сюжеты», бейджи «системный/мой» для карточек. Тесты обновлены под сюжеты (THEME_ID из plots, вес-инвентарь нейтрализован, изоляция graph-миров) — **все 175 pytest + механика 4/4 + node --check app.js.** Новый README для папки `plots/`.
**Сессия 29.1 (UI «Состояние» + карточки на старте + быстрые действия):**
- Фракции — карточки (как навыки) с кликом на модалку (`showFaction`): имя, статус репутации игрока (`repStanding`), связи с **именами** фракций (не id), описание. Хелпер `factionName(id)` резолвит id → имя из `setting.factions`.
- Магазины — в списке только имя/владелец/фракция (именем) + «товаров: N»; товары и цены — **внутри модалки** `showShop` (по клику).
- NPC — фракция показывается **именем** (не id) в списке и модалке.
- Флаги — читаемые ярлыки (`FLAG_LABELS` + `flagLabel`), кликабельные пилюли, модалка `showFlag`.
- Локация — в стиле «биографии» (`.loc-card`: название крупно, описание, рядом/станции).
- Убраны лишние «—» в начале small-строк навыков/эффектов/способностей и заглушка «ранг/тип: —» в `showEffect`.
- **Карточки знаний на старте**: `create_world` вызывает `ensure_knowledge_cards` сразу после применения `starting_state` — вкладка «Карточки» не пустая при создании мира из сюжета.
- **Быстрые действия не залипают**: `handleActionResult` обновляет `state.suggestions` ВСЕГДА (пустой результат → фолбэк от состояния); при открытии мира `loadSuggestionRefresh` перегенерирует варианты через новый `POST /api/worlds/{id}/suggest` (по последнему ответу рассказчика).
- Тесты: 175 pytest + механика 4/4 + node --check.

**Сессия 27 (🌐 Графовая база данных):** персистентный граф карты мира и связей сущностей вместо плоских `connections` в setting. Новый модуль `backend/graph.py` (чистые алгоритмы — BFS/достижимость/кратчайший путь — + синхронизация/JSON) и слой `backend/db.py` (таблицы `graph_nodes`/`graph_edges`, идемпотентная миграция в `_init_schema`) как источник истины рёбер. Синк `graph.sync_from_setting` перестраивает граф из `setting.locations` (рёбра канонически `source<=target`, без дублей) атомарно в транзакции каждого хода (`routers/core.py::_process_action`) и при создании мира. Эндпоинт `GET /api/worlds/{id}/graph?target=` — узлы с `current`, рёбра, слои BFS от текущей локации, кратчайший путь. CRUD: `graph_replace/graph_set_node/graph_connect/graph_nodes/graph_edges/graph_get_node/graph_clear_world`. Проверено вживую (живой мир: nodes/edges/current/path). Новые тесты: `tests/test_graph.py` (алгоритмы, слой БД, синк из setting, payload) + API-тест `/graph`. **Итого 160 pytest.** Отрисовка фронтенда по-прежнему использует `setting.locations[].connections` (граф — источник истины связей).

**Сессия 27б (расширение графа + перенос карты на граф):**
- **Типы узлов (kind) расширены**: кроме `location` добавлены `faction` (узел-хаб), `npc` (живые, связь `npc[].faction`), `shop` (связь `shops[].location` и `.faction`), `quest` (зарезервирован). `build_graph` строит желаемый граф из всех этих сущностей.
- **Инкрементальный синк**: `sync_from_setting` сравнивает желаемый граф с текущим (`_equal_snapshot`) и при неизменности НЕ трогает БД — нет лишних DELETE/INSERT каждый ход; при изменении — атомарный `graph_replace`.
- **Вкладка «Карта» и `/map` переведены на граф-эндпоинт**: `renderMap` читает `GET /api/worlds/{id}/graph` (узлы с `kind`/`current`, рёбра, слои, путь), кеш `mapGraph`/`refreshMapGraph`, фолбэк на `setting.locations`; не-локации (магазины/фракции/NPC) рисуются маркерами у локации-соседа. `/map` показывает локации + связанные сущности.
- **`graph_connect` безопасна**: пропускает само-петлю (a==b) и ребро на несуществующий узел.
- Проверено вживую (мир 44: `/map` выдал локацию + живого NPC). Новые тесты: `tests/test_graph_entities.py` (фракции/NPC/магазины, дифф-синк, безопасность connect, payload с kind). **Итого 164 pytest.**

**Сессия 22.5 (👁 Провидение — Божественный арбитр):** кнопка «Воззвать к Провидению» — мета-уровень, когда игрок видит ошибку Рассказчика (не выдали предмет, не списали ресурс). Отдельный строгий LLM-промпт (`narrator.divine_messages`/`divine_intervene`) сверяет жалобу с состоянием мира и последним обменом и, если ошибка реальна, правит мир директивами с сюжетным «искажением реальности»; если ошибки нет — мягко отказывает (`decline`) и НЕ читит. Эндпоинт `POST /api/worlds/{id}/divine` (`routers/worlds.py`), schema `DivineIn`, кнопка 👁 в строке подсказок (index.html `#btn-divine` → модалка с текстом `divineModal` → `doDivine`), события роли `divine` рендерятся золотым (`msgClass/WHO` + `.msg.divine` в style.css). Провидение — корректор мира: кулдаун `DIVINE_COOLDOWN_TURNS` по умолчанию **0 = можно воззвать при любой неточности сразу** (без ограничения по ходам; защита от злоупотребления — сами ответы: без настоящей ошибки Провидение отвечает decline и мир не трогает; `_divine_last_turn` в setting хранится только на случай включения кулдауна вручную). ⚠ **Изменено в сессии 36 (п.2): дефолт теперь 3 хода** — ответы модели защитой от фарма не являются; см. блок «Сессия 36». Ответы/воззвания индексируются в память. Проверено вживую: на бессмысленную жалобу Провидение ответило decline, состояние не тронуто. Тесты: `test_divine_messages_*`, `test_divine_intervene_*`. Итого 125 pytest.

**Сессия 23 (Фракции → геймплей, углубление):** репутация больше не только ценник в магазинах.
- **Ступени доверия** (`mechanics.reputation_standing`): Изгой(<−100) / Заклятый враг(≤−12) / Враг(≤−5) / Недоверие(≤−1) / Нейтрально(≤1) / Доверие(≤5) / Друг(≤12) / Союзник — единый ярлык доверия фракции (не моральная оценка).
- **Фракции и связи** `setting.factions` + `FactionHandler` (директивы `faction_add / faction_update / faction_remove`; id — тот же ключ, что reputation/npc.faction/shops.faction). У фракции — name/desc/alignment/relations ({other: союз|враг}).
- **Разлив репутации по связям** (`_apply_reputation_delta`): изменение репутации фракции переносится на объявленных в relations союзников (1/4 выгоды) и врагов (−вся дельта) — мир реагирует «по симпатии/антипатии». 
- **Правило 26** в system prompt: рассказчик ведёт диалог/реакцию/уникальные фракционные квесты строго по ступени игрока (Союзник — полное доверие/скидки/квесты; Враг — отказ, может выдать властям или атаковать).
- **UI**: вкладка «Состояние» — репутация со ступенями + блок «🏴 Фракции» со связями; в модалке NPC — фракция → ступень.
- Проверено: 133 pytest (в т.ч. 4 новых: standing, faction_add+relations, spillover, remove), JS `node --check`, сервер перезапущен.

**Сессия 22.6 (Профилирование/мониторинг + ИИ-качество):** новый модуль `backend/metrics.py` — in-memory реестр метрик (кольцевой буфер 500 + кумулятивные счётчики LLM-вызовов/времени/токенов). Сбор в `_process_action`: `llm_ms` (замер `time.monotonic` вокруг LLM-вызова), `prompt_tokens`/`completion_tokens` (по `est_tokens`; completion — от видимого `final_text`, т.к. в tools-режиме проза живёт не в `full`), `memory_tokens` (len RAG+лор //4), `repetition` — метрика ИИ-качества (`_repetition_score`, доля слов-дублей в ответе = зацикливание рассказчика), `provider`, `world_id`, `game_over`. Эндпоинт `GET /api/metrics` (`routers/system.py`) — агрегат (avg/max за всё и за 60-окно + распределение по провайдерам) и последние N. In-memory → сбрасывается при рестарте. Проверено вживую (ход записал llm_ms≈14с, completion 267, prompt 9804). Тесты: `test_repetition_score_*`, `test_metrics_record_and_aggregate`. Итого 129 pytest.

**Сессия 21 (игровая глубина B2–B5):**
- **B2. Карта мира — интерактив**: клик по дальней локации подсвечивает кратчайший путь от текущей (рёбра/узлы зелёные, `line.path`/`node.ispath`); фильтры «только достижимые» (затемняет изолированные, `_mapReachOnly`) и «подписи» (`_mapLabels`); кнопка «✕ путь». BFS-помощник `_mapPathIds` / `_mapWireFilters` (app.js), CSS в style.css.
- **B3. Ветвление/ступени/цепочки квестов**: `QuestHandler` получил `quest_advance` (по `steps` авто-вперёд/по имени), `quest_choose` (`chosen`/`branch`), и `quest_done {id, next}` (автозапуск следующего квеста либо активация существующего). Примеры в промпт и GAME_ENGINE_TOOL.
- **B4. Универсальные сверхспособности**: `player.abilities` (school/source/cost/desc) — `AbilityHandler` (ability_add/update/remove/use; use списывает MP). В display `format_state`, UI персонажа «⚡ Способности», знаниевые карточки, правило 18а в промпт, tool.
- **B5. Статистика и достижения**: `player.progress` (авто-счётчики kills/quests_done/discoveries/moves в EnemyHandler/QuestHandler/LocationHandler + `_bump`; `progress_add`), `player.achievements` (achievement_add), команда `/stats`, блок «📊 Статистика / 🏆 Достижения» в персонаже. Схема: `ensure_player_schema` добавил `progress`/`achievements`.
- Проверки: 115 pytest (в т.ч. новые quest_branch_* / setup_progress / ability / achievements), механика scripts/test_directives.py 4/4, JS `node --check`, сервер перезапущен.

**Сессия 20 (надёжность A1–A3 + декомпозиция C1):**
- **A1. Логирование вместо тихих `pass`**: в фоновом/асинхронном пути (`routers/core.py`: TTS-задача, карточки знаний, автосохранение, восстановление снепшота, нормализация директив; `narrator.py`: карточки знаний, синхронизация квестов, индексация карточек в Chroma) `except Exception: pass` → `log.warning(...)` с контекстом (world_id/seq). Легальные парсер-фолбэки (ожидаемый не-JSON от LLM) остались тихими.
- **A2. Устойчивость судьи логики**: `narrator.logic_judge` переписана пошагово — при невалидном JSON повтор с пониженной температурой (0.2→0.05), затем фолбэк-пропуск с `log.warning` (не молча).
- **A3. Уведомление о деградации памяти**: `routers/core.py::_rag_note` — если эмбеддинги выключены и RAG вернул пусто, в `memory_used` добавляется подсказка «Память понижена: эмбеддинги выключены» (в «🧠 Память» окно; только при пустом + выкл., обычный пустой поиск не шумит). Фронт рендерит kind «⚠».
- **C1. Декомпозиция `narrator.py`** (2750 → 2066 строк): статические данные `THEMES`, `GENRE_HINTS`, `NARRATOR_PRESETS` вынесены в `backend/narrator_data.py`; `narrator.py` импортирует их (`from .narrator_data import ...`) — все внешние `narrator.THEMES/GENRE_HINTS/NARRATOR_PRESETS` продолжают работать. Тесты: 110 pytest (в т.ч. новые A2/A3), механика 4/4, сервер перезапущен.

**Сессия 19 (ROADMAP-обновление — планируемые работы):** ROADMAP.md пополнен новыми целями. Кратко, чтобы будущая сессия видела картину:
- **Надёжность**: в фоновых задачах (память/карточки/сводки/судья логики/события) заменить тихие `try/except: pass` на `logger.exception(...)` с деталями (world_id, seq, исключение); явная подсказка пользователю/в аудите памяти при недоступности эмбеддингов/Chroma (сейчас — молча пустой RAG); если `logic_judge` вернул невалидный JSON — повтор с пониженной температурой или фолбэк с логированием (сейчас молча игнорируется).
- **UX**: интерактивность карты мира (подсветка путей, фильтры, зум/панорама уже есть — см. сессию 7.1).
- **RPG-глубина**: фракции/репутация влияют не только на цены (уникальные квесты/диалоги/реакции NPC); ветвление и условия прогресса в сюжетных линейках (сейчас квесты — простые флаги).
- **Код**: декомпозиция `narrator.py` (`build_system_prompt` >200 строк и др. длинные функции — вынести правила/жанры/форматы директив).
- **Технические**: WebSocket вместо SSE-поллинга; графовая БД (Neo4j/NetworkX) для карты и связей сущностей вместо плоских `connections`; LRU-кеш эмбеддингов; асинхронный ChromaDB-клиент (сейчас sync-httpx в async-функциях может блокировать цикл); профилирование/мониторинг (время LLM, токены, размер памяти).

- Тематика: **сюжеты-миры файлами в `plots/`** (раньше 36 тем в хардкоде, теперь — папки `system`/`user`, JSON по `PLOTS.md`) / **33 жанровые подсказки** (добавлены литрпг и реалрпг; 4 мира по произведениям: Фантазия/FFF-Trashero, Айнкрад/SAO, Ключи Пангеи, Хризалида(=Безграничный, Голд) — ранее были темами, теперь их надо класть файлами в `plots/`). Фантазия/Айнкрад/Хризалида/Ключи Пангеи имеют развёрнутый лор и стартовую локацию (start_location).
- Рассказчики: **7 пресетов** (Классический/Строгий/Поэт/Наставник/Хоррор/Циник/Режиссёр) + можно создавать свои; **описание показано при создании мира** при выборе.
- Мультижанр: при создании мира можно выбрать несколько жанров (чипы из `GENRE_HINTS`) — записываются в `worlds.genre` через запятую, в промпт попадают совмещённые жанровые правила.
- **Свой сюжет**: первый пункт в списке тем — кастомная завязка с генерацией открытия (`theme='custom'`); «Мои сюжеты» — сохранённые заготовки с добавлением/правкой/удалением (таблица `story_plots`, `/api/plots`).
- Счётчик ходов в списке миров — только действия игрока (`role='player'`); сложность и название темы — на русском.
- **Обратная связь влияет на стиль**: лайки/дизлайки (последние ~6 оценок) подмешивают рассказчику намёк в system prompt (`feedback_style_note`).
- UI: кнопка «← Меню» в мире; вкладка Настройки — сворачиваемые разделы со слайдерами на всю ширину (max_tokens до 4096, ползунок «Размер контекста» до 262144/256k с выводом бюджета памяти и описаниями полей); кнопка ↻ у ответов рассказчика — перегенерация без нового хода; сообщение кубов — многострочное и понятное.
- Провайдеры: main = openai_compat (RouterAI, solar-pro4), embedding = RouterAI, rerank = RouterAI (включён); per-world переопределение работает.
- Смена рассказчика и провайдеров проверены (UI настройки), `start_game.bat` чинит «порт уже занят».
- Миры в БД: тестовые (можно удалять). Chroma-чанков ~13.
- Рабочие миры/слоты/экспорт/настройки/мастер/фидбек — проверены.

**Сессия 18 (время/погода → геймплей + расписания NPC):** `narrator.environment_mods(time, weather)` — универсальные геймплейные эффекты времени суток и погоды (ночь: −видимость/+скрытность; туман/гроза/буря/мороз/жара и др.), выводятся «Влияние среды» в `format_state` + правило 24 промпта (рассказчик учитывает в описаниях и roll.mod). `NpcHandler` поддерживает `npc_set.schedule` (`{время: действие}`); `npc_schedule_text(setting, id, npc)` показывает активную запись под текущее время в состоянии + правило 25 промпта; сайдбар UI (app.js `renderSetting` npc-list) показывает расписание у живых NPC под `s.time`. End-to-end проверено (директива time/weather/npc_set.schedule применяется через полный API-ход).

**Сессия 17 (прозрачность RAG — «🧠 Память»):** `routers/core.py` возвращает в результате хода `memory_used` — список подхваченных фрагментов памяти/лора (`_memory_audit(rag_chunks, lore_chunks)`, kind «Память»/«Лор», до 20 фрагментов по ~220 симв.). Фронт (`app.js` handleActionResult + `style.css`) под последним ответом рассказчика добавляет кнопку «🧠 Память (N)» → модалка со списком фрагментов. **Персистентность: `memory_used` сохраняется в колонке `events.meta` (JSON, `db.add_event(..., meta=...)`) у события рассказчика, и `buildMsg` рендерит плашку из `e.meta.memory_used` — поэтому «🧠 Память» остаётся у ответа и после перезагрузки страницы / пагинации / опроса (`events_since`/`history`/`world_detail` отдают `meta`). У старых событий (без meta) плашка не рисуется.** Универсально для всех жанров. + Отложенная задача «Магическая система» в ROADMAP переформулирована как **универсальная** «Система сверхспособностей» (не сугубо магия — реал/техно/космос без неё).

**Сессия 16 (асинхронная БД aiosqlite):** `backend/db.py` переведён на **aiosqlite** — единственное соединение живёт на фоновом asyncio-цикле (поток `aiosqlite-loop`), публичный API модуля остался синхронным (`_run`/`asyncio.run_coroutine_threadsafe`), поэтому роутеры/narrator/tts и тесты не менялись. Сохранены RLock + `_tx_depth` + `BEGIN IMMEDIATE` (атомарность хода). `_maybe_commit` стал корутиной (в корутинах коммит напрямую `await conn.commit()`, не через `_run`, — иначе само-деадлок). Добавлен `db.close()` для тестов/скриптов. Регрессия: 106 pytest + 4/4 test_directives + живой сервер :8002.

**Сессия 15 (богатый старт персонажа + блок «Персонаж» + кликабельные предметы):**
- **Богатая первая генерация**: `narrator.generate_character` (доп. LLM-вызов при создании мира, `max_tokens=2400`; колбэк через `apply_character`) заполняет: имя, биографию (до 1600 симв.), расу/класс/профессию (через движок директив — бонусы/пассивки/стартовые навыки/баффы), уровень (1..99 по биографии/лору — бессмертный маг 900 лет не стартует новичком), характеристики (3..20, НЕ все 10), навыки, титулы, персонализированный инвентарь 2–6 предметов с описаниями. Если игрок дал зацеп — identity из него (приоритет), фолбэк — `generate_identity` (лимит тоже поднят до 1600, `max_tokens=500`).
- **Блок «🧙 Персонаж»** во вкладке «Состояние» (перенесён после «Характеристик», переименован из «Кто ты» в «Персонаж»): имя + полная биография («char-bio», не режется) + раса/класс (ранг)/мультикласс/профессия/навыки (с описаниями)/титулы/репутация/действия — отдельными явными параметрами с описаниями (справочники `CHARACTER_RACE_DESC/CLASS_DESC/PROF_DESC` в app.js).
- **Премьи/фреймы**: блоки HP/уровня/золота/погоды (`player-kv`) и «Характеристики» (`stats-kv`) обёрнуты в ту же рамку `.card-frame`, что и биография `.char-card`.
- **Кликабельные предметы**: предметы инвентаря — кликабельные (`showItem`), открывают модалку с полным описанием (имя, qty, desc); без описания показывается подписка-маленькая + «Описание отсутствует» в модалке.
- **Имя** присутствует в блоке «Персонаж» (заголовок карточки) и в первом сообщении (открытие `generate_opening` чётко представляет персонажа по имени, токены открытия выросли).

**Сессия 5 (RPG-система, сессия по 1.txt):**
- **7 статов + производные** (макс. HP/MP от статов), аддитивные `player.stats`;
- **Расы/классы/профессии**: 8 рас (бонусы+пассив), 6 классов (стартовый навык при смене, ранг F..G, эволюция, мультикласс), 9 профессий (постоянный бафф); все — вшитые **и** творческие (race_change/class/profession с полями bonus/passive/skill/buff); смена откатывает старый бонус/пассив/буфф;
- **Навыки с рангами** F..G (skill_add/skill_rank/skill_remove, старый формат конвертируется);
- **Эффекты**: постоянные/временные, стаки (урон×стаки в тике), моды к статам (effective_stats показываются рассказчику и в UI), tag для пассивок;
- **Откат при перегенерации по снапшоту** (`worlds.snapshot`): тик эффектов не задваивается, событие игрока не дублируется;
- **Карточки знаний** в памяти: `ensure_knowledge_cards` вызывается в потоке хода (инлайн) + индексация в Chroma в фоне; `select_relevant_entities` учитывает знания (бонус приоритета); UI вкладки «Карточки» и «Состояние» показывают карточки/расы/ранги/стаки/моды;
- **Фикс RAG**: `embeddings.embed_query/embed_documents` теперь принимают `provider` (per-world провайдеры работали только для индексации знаний, retrieval молча падал с TypeError → пустой результат; теперь индексация обменов и retrieval работают);
- **Системный промпт**: правила 14–18 (статы/расы-классы-ранги/эффекты/флаги/«назначь расу и класс»), примеры директив обновлены;
- E2E: мир → мастер-патч (раса/класс/навык/эффекты) → ход (тик −2 HP) → регенерация (HP не задваивается) → карточки знаний в SQLite+Chroma → поиск памяти возвращает карточки. Тестовый мир удалён.

**Сессия 5 (по 1.txt, продолжение):**
- **Профессии растут от действий — но авто-смена отключена (сессия 19)**: `check_profession_advance` убрана из конвейера; профессию/класс/титулы изобретает и применяет рассказчик (правило 19, директивами profession/class/title). Добавлен `player.level` (явный сюжетный рост уровня).
- **Скрытые классы/расы/профессии/навыки**: правило 19 в системном промпте — творческие сущности выдаются за цепочки действий/испытания, условия не афишируются; **условие получения фиксируется на карточке** (meta.origin из системных сообщений хода); карточки предметов инвентаря тоже создаются (kind=item); `compact_entity` показывает origin.
- E2E: мастер-патч actions → ход → авто-профессия «Кузнец» с баффом + системные сообщения; карточка профессии с origin; перегенерация: состояние корректное (профессия + buff не задваиваются).

**Чтобы проверить «живость» сервера:** `curl -s http://127.0.0.1:8002/api/system/status`.

**Сессия 4 (статус-эффекты, статы, классы, флаги, жанры):**
- Статус-эффекты игрока: `player.effects {имя: {turns, damage, heal, desc, mods}}`, директивы `effect_add/effect_remove`, автотик в начале хода (`tick_effects` в `_process_action`, не при перегенерации), смерть от эффекта → game over.
- Статы/класс/профессия/навыки/титулы/репутация: `player.class`, `player.profession`, `player.skills {имя: уровень}`, `player.titles []`, `player.reputation {}`; директивы `class/profession/skill/skill_remove/title/reputation/stats`; показ во вкладке «Состояние» (Характеристики, Роль и навыки, Эффекты).
- Флаги в UI человекочитаемые: `дверь_открыта: да/нет` (вместо `door_open=true`) + подпись-пояснение; системный промпт объясняет рассказчику природу флагов.
- Блок жанров больше не обёрнут в `<label>` (`div.field-block`) — клик по тексту больше не включает первый жанр случайно; у чипов явный стиль `:focus-visible`/`:active`.

---

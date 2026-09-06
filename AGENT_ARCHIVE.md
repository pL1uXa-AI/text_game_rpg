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

---

# 📦 Часть 2 — эссе и отчёты, перенесённые из ЖИВЫХ разделов `AGENT.md` (сессия 62, C4)

> Сессия 62 (аудит 41, пункт C4) вынесла сюда отчёты сессий 6–40, которые жили внутри
> «АРХИТЕКТУРЫ», «СОГЛАШЕНИЙ ПО КОДУ» и «СОСТОЯНИЯ И ИСТОРИИ» `AGENT.md` — файла, который
> обязаны читать ПЕРЕД любой правкой. Действующее правило от каждого эссе осталось в
> `AGENT.md` короткой строкой (там же стоят ссылки на этот раздел); здесь — мотивы, замеры,
> «симптом → причина» и списки регрессий. Текст **дословный**: номера строк, цифры и устаревшие
> пути оставлены как были. Правильные числа теперь измеряет `scripts/doc_figures.py`, и этот
> файл в его реестр НЕ входит: устаревшая цифра в отчёте «что было» — часть истории.

## 🎮 Сессия 40 — баг-хант мира «Новый мир» (п.1–п.16)

### Директивы: канал MP у эффекта (п.1) и `item_update` (п.2)
*(из `AGENT.md`, «АРХИТЕКТУРА → Директивы `<<ENGINE>>`»)*

  - **Сессия 40, п.1 (мир «Новый мир»): у эффекта появился второй тикающий ресурс — энергия.** РАНЬШЕ `tick_effects` умел списывать/лечить только HP (`damage`/`heal`), поэтому «Кристальная лихорадка: −30 HP, −20 MP за ход» выжигала по чуть-чуть HP и не трогала MP: модель честно писала убыль текстом в `desc`, а движку там брать было нечего (MP уходила лишь когда модель сама догадывалась дать `player.mp` — и немима, без системки, отсюда же ощущение «эффекты не применяются», п.15). Теперь: `mp_damage`/`mp_heal` — MP за ход (алиасы `mp_cost_per_turn`/`mp_restore`), тик по ним даёт `⏳ Эффект «…» : −N MP → x/max`, каналы видны в карточке эффекта, в модалке, в `format_state` и в системке наложения. Семантика не меняется: движок тикает ресурс, сюжетные последствия — на рассказчике (закон 3). Там же `player.mp` перестал меняться молча — даёт `Энергия: x/max (±N)`, как hp/gold/xp. Для уже созданных миров (где числа живут только в `desc`) тик чинит себя сам: `mechanics._ticks_from_desc` добирает убыль из текста при явной фразе «за ход»/«/ход» и только в пустые каналы (`tick_effects`), поэтому «Кристальная лихорадка» в мире №103 теперь жжёт и HP, и MP. Тесты: `test_tick_effect_mp_drain`, `test_tick_effect_mp_from_desc`, `test_player_mp_message`, `test_effect_add_mp_damage_and_label`.

- `item_update: {name, desc, new_name, weight, value, note}` — **правка уже выданного предмета** (сессия 40, п.2). Нужна потому, что `add_item` дописывал `desc` только в пустое поле и не трогал существующий предмет: рассказчик, подробно описавший найденный кристалл в тексте двух последующих ходов, не мог закрепить это в инвентаре — «у кристалла так и не появилось описание». Теперь `ItemHandler` дописывает `desc` (не затирая прежнее, дубликаты не плодит), меняет `new_name`/`weight`/`value`/`qty`, копит `notes`; нет предмета в инвентаре → `⚠ Нет предмета …`, успех → `📖 Изучено: …`. Карточка знания предмета (`memory.ensure_knowledge_cards`) при пустом `desc` больше НЕ затирает наведённое описание заглушкой «в инвентаре игрока» (`summary=None` = не трогать), а `format_state` помечает предметы без описания как «(описание не известно)», чтобы модель видела пробел и закрыла его через `item_update`. Промпт: правило 18а + ключ в `GAME_ENGINE_TOOL`, в audit-промпте и в промпте Провидения (жалоба на пустое описание правится `item_update`, а не повторной выдачей). Тесты: `test_item_update_desc`, `test_item_update_normalization`, `test_item_card_gets_description_later` (e2e).

### Автогенерация контента и фронтенд: п.3–п.16
*(из `AGENT.md`, «АРХИТЕКТУРА → Автогенерация контента»)*

  - **Сессия 40, п.3 (мир «Новый мир»): вступление больше не уходит в чат обрывком.** Симптом был «первого сообщения рассказчика не было», а в БД оно лежало — но обрезанное до 409 симв. и обрванное посреди фразы: «…в свободной колонии «Осколок Рассвета» (для сравнения — другой мир того же сюжета: 2442 симв.). Причина: страховка от обрыва проверяла `res[-1] not in ".!?…»"`, а в русском закрывающая кавычка `»` — НЕ конец мысли (нужна точка ПОСЛЕ неё), поэтому обрывок проходил как «законченный» и модель не переспрашивалась. Теперь: `character_generator._ends_sentence()` (срезает хвостовые кавычки/скобки/пробелы и смотрит на `.!?…`; само `…` хвостом НЕ считается), генерация повторяется до 3 раз, учитывается `finish_reason == "length"` (упёрся в лимит = точно не дописано), а если законченного текста так и не дали — берётся обрезок до последнего ПОЛНОГО предложения (`_cut_sentence`, `»` из его символов убран) и лишь при нечего резать — завязка сюжета. Всё сопровождается `log.warning` (правило 14). Регрессии: `test_ends_sentence_russian_quotes`, `test_opening_retries_truncated_text`, `test_opening_never_returns_fragment` (tests/test_session38_bugfixes.py).
- **Сессия 40, п.4 (мир «Новый мир»): текст рассказчика снова читается (и он больше не «перерисовывается другим видом» в конце хода).** Симптом: стрим печатал ответ нормальным текстом, а по завершении хода сообщение исчезало и всплывало тусклым, мелким и «сливающимся с фоном». Причина чисто фронтовая, регрессия аудита 38 (E2): `msgClass` лишился явной ветки `narrator` — дефолт `return "unknown"` (новый «неизвестная роль») стал срабатывать и для рассказчика, а `.msg.unknown .body` — это `color: var(--muted,#9aa)`, 13px и пунктирная рамка. Живой же `addTyper()` ставит `msg narrator typing`, поэтому до перерисовки стиль был правильным. Лечится одной строкой: `if (role === "narrator") return "narrator";` — `msgClass` по-прежнему возвращает `unknown` для незнакомых ролей (роль E2 сохранена). Регрессии: `test_msgclass_maps_narrator_explicitly`, `test_narrator_style_is_readable_not_muted` (tests/test_session40_bugfixes.py).
- **Сессия 40, п.5 (мир «Новый мир»): у хода игрока сразу появляется номер «#N».** Симптом: «Вы #» без номера, а после F5 — с номером. Причина: сообщение игрока рисуется **оптимистично** (`sendAction`/`sendAsAction`/`quickAction` зовут `appendMsg({role:"player", seq:""})`, чтобы отклик был мгновенным) и не знает ни id, ни seq события; шаблон `buildMsg` печатал `<span>#${e.seq || ""}</span>` **всегда**, отсюда пустая решётка. Настоящее player-событие приходило в `res.events`, но `handleActionResult(…, skipPlayer)` отбрасывал его как «уже нарисованное», а дедюп `appendMsg` по `data-id` его бы всё равно не спас (локальному пузырю выдан временный id `local-N`). Теперь: (а) пустой seq не рисуется вообще; (б) новый `adoptPlayer(e)` **усыновляет** пузырь — подставляет в уже нарисованный элемент реальные `id`/`seq`, дорисовывает «#N» и поднимает `state.seenSeq`; вызывается в `handleActionResult` ДО фильтра `skipPlayer`. Пузырь ищется совпадением текста с конца (пробелы/`<<ENGINE>>` нормализуются; не «последний локальный»: пузыри слэш-команд помечаются `data-local` и в выборку не попадают, а при единственном пузыре без совпадения берётся он — сервер мог срезать механику из текста). Побочный эффект: события перемотки/дневника теперь сопоставимы с живым чатом до перезагрузки. Регрессии: `test_buildmsg_hides_empty_seq`, `test_action_result_adopts_player_event`, `test_adoptplayer_real_js` (тот же путь на Node по извлечённой из исходника функции, с деградационной пробой на чужой пузырь).
- **Сессия 40, п.6 (мир «Новый мир»): компас «🧭 рядом:» шлёт человекочитаемое название локации.** Симптом: нажатие на «→ Тропа к Форпосту» подставляло во ввод `Идти в path_to_architects` — в чате навсегда оставалась реплика игрока с машинным id, и модель описывала переход по нему. Причина: `renderCompass` шёл в обход общей формулировки перехода и брал `data-loc` (внутренний id), тогда как карта (`renderMap`) и фолбэк быстрые действия всегда слали имя («Я иду в «…».»). Теперь: (а) новый `travelActionText(name)` — ЕДИНЫЙ конструктор фразы перехода, его зовут компас, карта и быстрые действия (тест требует, чтобы шаблон фразы в `app.js` остался ровно один); (б) на кнопке появляется `data-name`, `data-loc` остаётся для отладки; (в) ребро считается висячим (и кнопкой не делается) только если цели нет НИ В ОДНОЙ карте мира — ни в `setting.locations`, ни локацией в графе: механика `move` валидирует переход по id (`mechanics.LocationHandler`), так что безымянная, но существующая локация остаётся видимой под своим id, а вот призрака компас показывать не должен; (г) фолбэк-ветка компаса по графу читала рёбра как кортежи (`e[0]`/`e[1]`), а сервер отдаёт словари `{source,target}` — фолбэк был мёртв, теперь исправлен и фильтруется по `kind === "location"` (в графе есть `npc:`/`shop:`/`faction:`-узлы); компас перерисовывается по приходу графа (`refreshMapGraph`). Регрессии: `test_travel_action_text_helper_exists_and_is_human`, `test_compass_button_carries_name_and_sends_it`, `test_compass_name_resolution_real_js` (Node, данные мира №103).
- **Сессия 40, п.7 (мир «Новый мир»): бессрочный эффект со сроком в описании больше не висит незамеченным.** Симптом: «Стабилизация родового канала» — «требуется ещё 1-2 сессии медитации», но `turns: -1` (бессрочно) и виси сколько хочешь. Причина НЕ в движке: модель наложила эффект без `turns` (правило 16 разрешает: -1/нет = постоянный), а срок написала только в `desc`, который `tick_effects` не читает. Движок НЕ ставит turns сам и не снимает эффект «по тексту» — это решение мастера (закон 3). Сделаны три вещи: (а) **профилактика** — в правило 16 system prompt и в описания инструмента `game_engine` добавлено: назвал в `desc` срок/условие — задавай и `turns`; (б) **сигнал при наложении** — `mechanics.effect_needs_turns` (`turns` пустой/−1 И в `desc` есть срок по `_duration_hint`) даёт системку «⚠ Эффект «…» бессрочен (turns не задан)… повтори effect_add с turns=N или сними effect_remove»; (в) **сигнал каждый ход** — `format_state` помечает такой эффект как `постоянно ⚠в описании срок: задай turns или сними effect_remove` (разовая системка при наложении могла уйти в историю). Плюс найденный рядом **настоящий баг памяти**: `format_state` резал `desc` эффекта жёстко `[:70]`, и фраза со сроком (она почти всегда во ВТОРОЙ половине) до модели не доходила вовсе — мастер не мог увидеть причину. Теперь описание передаётся **ЦЕЛИКОМ**: `mechanics.desc_compact(desc, limit=None)` по умолчанию ничего не ужимает (нормализует только пробелы; явный `limit` = сжатие по предложениям с полным сохранением последнего), `format_state` зовёт его без лимита, а потолки самого `desc` подняты с 200 до 500 знаков (`EffectHandler`, `apply_location_effects`) и снят обрез `[:150]` в системке наложения. Цена — токены: `desc` входит в `format_state` каждого хода, поэтому за окно отвечает `test_build_messages_never_overflows_window` (реальное описание эффекта ≈130 зн., эффект в промпте один). Заодно `turns` упомянут в схемах `GAME_ENGINE_TOOL`/audit-промпта. Побочное, замеченное тестом: удлиннение правила 16 упиралось в бюджет окна (`test_build_messages_never_overflows_window`) — формулировка сжата. Регрессии: `test_duration_hint_detects_spoken_deadline` (в т.ч. ложные срабатывания на «−30 HP, −20 MP за ход»), `test_effect_add_warns_when_permanent_but_timed` (плюс — движок НЕ сам проставляет turns), `test_readd_with_turns_fixes_permanent_effect` (подсказка исполнима), `test_format_state_shows_full_effect_deadline`, `test_format_state_marks_permanent_timed_effect`, `test_prompt_rule_requires_turns_for_timed_effects` (тест проверяет смысл правила, а не цитату — формулировку может сократить будущий аудит бюджета).
- **Сессия 40, п.8 (мир «Новый мир»): один эффект больше не заводится под именем-синонимом незамеченным.** Симптом: «Стабилизация фрактала» (ход 20), «Стабилизация родового канала» (ход 24) и «Стабилизация узора» (ход 35) — три названия ОДНОГО процесса стабилизации, наложенные как три разных эффекта и висящие бессрочно (у «Кристальной лихорадки» носитель не менялся, а «лечений» накопилось три, с разными desc). Причина НЕ в движке: эффекты — словарь `player.effects`, ключ = строка `name` из `effect_add`; перефразировала модель имя — получился новый ключ. Движок НЕ сливает и НЕ переименоывает эффекты сам (закон 3: «Стабилизация фрактала» и «Стабилизация узора» могут быть и осознанными разными состояниями). Сделано три вещи, всё — сигнал (закон 2): (а) `mechanics._same_effect_name` / `find_similar_effect` — узкий эвристический тест имени (общее первое слово-заголовок + `difflib` ratio ≥ 0.6; при разном заголовке ≥ 0.9), ловит реальные синонимы и НЕ склеивает «Ледяная броня»/«Ледяной щит», «Ярость берсерка»/«Покой берсерка», «Благословение луны»/«Проклятие луны», «Второе дыхание»/«Дыхание зимы»; (б) системка при наложении нового эффекта с близким именем — «⚠ Имя «…» близко к уже висящему «…». Если это то же состояние — не держи дубль: обнови прежний эффект его именем либо сними старое effect_remove; если состояние другое — назови иначе» (формулировка — ВОПРОС, решение за мастером); (в) та же пометка в `format_state` (`⚠возможен дубль имени…`) — разовая системка могла уйти в историю, а «Эффекты» мастер видит каждый ход; плюс в правило 20 промпта добавлено «ОДНО состояние = ОДНО имя». Регрессии: `test_same_effect_name_pairs_dupes_but_not_distinct_states` (в т.ч. отрицательные пары), `test_effect_add_warns_on_duplicate_name` (плюс — движок НЕ сливает эффекты), `test_format_state_hints_duplicate_effect_names`, `test_prompt_rule_forbids_synonym_effect_names`. Побочное: тест окна (`test_build_messages_never_overflows_window`) поймал, что удлиннение правила 20 упиралось в бюджет — формулировка сжата (тот же эффект, что  в п.7 для правила 16).
- **Сессия 40, п.9 (мир «Новый мир»): в карточках больше нет системных (машинных) имён.** Симптом: во вкладке «Карточки» — `player` и `trail_to_outpost` вместо «Игрок» и «Тропа к Форпосту» (в БД на момент правки таких карточек было 8 в трёх мирах). Причина НЕ во фронте: `name` рисётся как есть, а его портил фоновый архивариус — `memory.update_entity_cards` подставлял ключ, когда LLM присылала только `{kind, key}` без `name`: `name = str(item.get("name") or key)`. Машинный id затем попадал и в контекст модели (`compact_entity` даёт имя карточки в промпт и в RAG), так что дефект был не только косметическим. Движок по-прежнему НИЧЕГО не выдумывает (закон 3) — он достаёт имя, которое мир уже знает (закон 2): новый `memory.resolve_card_name` с приоритетом «имя из ответа архивариуса → имя из состояния мира (`card_name_from_setting`: npc/locations/quests/shops/enemies/companions/crafts/factions, поиск и по ключу словаря, и по полю `id`) → прежнее имя карточки → сам ключ», и `looks_machine_name` (только латиница/цифры/`_-.` без пробелов — русские названия и вид «V-28» под него не попадают) запрещает машинному имени перетереть уже человекочитаемое (поздний проход архивариуса без `name` больше не откатывает «Игрок» → `player`). Профилактика в источнике: в промпт архивариуса добавлено «ОБЯЗАТЕЛЬНО заполняй name — человекочитаемым именем на языке мира». Нижний слой `db._upsert_entity` (`name or entity_key`) не тронут — это страховка, а не источник имени. Уже записанные карточки чинит идемпотентное само-исцеление при старте сервера (`memory.repair_machine_card_names`, вызывается в `app.py` рядом с `prune_orphan_rows`/`normalize_setting_ranks`): правит ТОЛЬКО карточки с машинным `name`, у которых состояние мира знает настоящее имя той же сущности (dry-run по боевой БД: мир 103 `npc/player` → «Игрок», три квеста мира 54 → их `title`; мир 80 `location/kolodets` не тронут — имени в состоянии нет, выдумывать нечем). Регрессии: `test_looks_machine_name_only_catches_ids`, `test_card_name_from_setting_reads_world_state`, `test_resolve_card_name_prefers_human_and_blocks_machine_overwrite`, `test_repair_machine_card_names` (в т.ч. идемпотентность и «не трогать человекочитаемое»), `test_archivist_prompt_requires_human_readable_name` (tests/test_session40_p9_cards.py).
- **Сессия 40, п.10 (мир «Новый мир»): клик по записи дневника перешёл к ходу сам.** Симптом: ЛЮБАЯ запись вкладки «Дневник» давала `Событие хода N не в загруженной части лога. Поднимись в начало и нажми «⬆ Показать ранние события»` — то есть переход в прошлое, ради которого и сделан дневник, требовал ручной пагинации чата. Причин было две, и обе — фронтовые: (а) `scrollToSeq` искал событие **среди отрисованного** и, не найдя, ничего не делал сам; при открытии мира в чат грузится только хвост лога на 60 событий (`worlds.py::world_detail` → `db.get_events(limit=60)`), так что в мире длиннее страницы ни одна ранняя запись не открывалась никогда; (б) событие могло и не быть в DOM при полностью загруженном логе: у оптимистичного пузыря игрока `data-seq` пустой до `adoptPlayer` (п.5 этой же сессии), а перемотка/`↻` вообще удаляют события хода — алерт врал, что «не в загруженной части». Теперь: (а) `loadEarlier()` возвращает `true/false` (догрузил/всё), и `scrollToSeq` — `async`, сам догружает ранние страницы (потолок `_SEQ_JUMP_MAX_PAGES = 40`, чтобы не зациклиться на сервере, который вечно отдаёт то же) и после каждой страницы пробует перейти; (б) если точного хода в логе нет — ведёт к **ближайшему сохранившемуся** (`_nearestSeqMsg`) и честно называет причину (перемотка/не показывается в чате), а не отправляет к мёртвой кнопке; (в) запись без привязки к ходу (`seq = 0`) больше не даёт бессмысленное «хода 0». Законов не затрагивает: только навигация/отображение UI, данных и механика не менялись. Регрессии (tests/test_session40_p10_journal.py): `test_scroll_to_seq_real_js` — НАСТОЯЩИЕ `scrollToSeq`/`_focusSeqMsg`/`_nearestSeqMsg` на Node с пятью сценариями (ход на экране без лишних запросов; живой «запись хода 6 при хвосте от 20»; отсутствующий ход → ближайший; `seq=0`; «застрявшая» пагинация не зацикливает), `test_scroll_to_seq_no_longer_sends_player_to_the_button`, `test_load_earlier_reports_whether_it_loaded_more` (текст-тупик закреплён в отказе — вернуться он не может), `test_journal_entries_carry_jumpable_seq` (роль `player` обязана оставаться в `CHAT_LOG_ROLES`: ссылки дневника ведут именно на ходы игрока).
- **Сессия 40, п.11 (мир «Новый мир»): в статистике пути игрока нет машинных ярлыков.** Симптом: в карточке персонажа — «📊 Статистика: Moves 1 · Discoveries 1», в `/stats` — «Пройдено: moves 1; discoveries 1» (в БД мира №103 так и лежало). Причина НЕ в данных: движок сам ведёт четыре счётчика машинными ключами (`mechanics._bump`: kills/quests_done/moves/discoveries), а UI печатал ключ «как есть, только с заглавной» (`ucfirst(k)` в `renderSetting`) и `/stats` (`routers/worlds.py::_slash_stats`) — тоже. Тот же класс дефекта, что п.9 (машинные имена в карточках), но другой слой. Ключи в `setting` СОЗНАТЕЛЬНО не переименованы: на машинные id опираются промпт (`progress_add`, правило 20а), `format_state` и все старые сохранения — правь их = ломай данные ради косметики. Сделан только перевод на человекочитаемое при показе (закон 2): `mechanics.PROGRESS_LABELS`/`progress_label()` — единый источник ярлыков для ЧЕТЫРЕХ счётчиков, которые заводит код; `/stats` берёт его, фронт — свою копию `PROGRESS_LABELS`/`progressLabel()` (расхождение двух списков ловит тест). Ключи, придуманные мастером через `progress_add` («победы», «Дуэлей выиграно»), выводятся КАК ЕСТЬ: перефразировать имя, данное рассказчиком, — решение за мастером, не за кодом (закон 3). Регрессии (tests/test_session40_p11_stats.py): `test_bump_keys_are_the_ones_we_label` (словарь не устаревает молча: все ключи, что пишет `_bump`, обязаны иметь ярлык), `test_progress_label_readable_and_conservative`, `test_stats_slash_shows_labels_not_keys` (живой TestClient + `/stats`), `test_progress_label_real_js` (НАСТОЯЩИЙ `progressLabel` из app.js на Node), `test_frontend_and_backend_labels_agree`, `test_keys_untouched_in_state` (данные и промпт остались машинными).
- **Сессия 40, п.12 (мир «Новый мир»): у флага появилось человекочитаемое название.** Симптом: в «Состоянии» — «Player awakened: да», «Core sealed: да», в модалке — ключ `player_awakened`, в дневнике — «Так в мире и осталось: core_sealed». Третий случай того же дефекта после п.9 (карточки) и п.11 (статистика). Причина НЕ во фронте: `flagLabel` честно перебивал известные ключи словарём `FLAG_LABELS`, но тот был разово захардкод под один сюжет, а ни рассказчик, ни файл сюжета имени флагу дать не могли — у нового флага оставался «id со звёздочками». Ключи СОЗНАТЕЛЬНО не переименованы: на машинные id опираются судья логики, директива `flag`, `station:`-флаги крафта (`location_stations`) и все старые сохранения (закон 3 — имя даёт мастер, не код). Сделано: имя хранится ОТДЕЛЬНО от значения — `setting["flag_titles"]` {ключ → название},Имя приходит: (а) директива `flag {name, value, title}` (`WorldHandler`, пустой `title` прежнее имя не затирает, потолок 120 зн.), (б) сюжет: `starting_state.flags` теперь принимает и `{"value": true, "title": "…"}` (старая форма `ключ: true` валидна, `apply_plot_start`), вёлся в `PLOTS.md` п.4.4; (в) режим «Мастер» — `flag_titles` добавлен в белый список `state/patch`. Показ: `flagLabel` фронта читает `state.setting.flag_titles` ПЕРВЫМ (потом свой словарь, потом адаптивный fallback), модалка при отсутствии имени честно говорит, что его задал бы мастер; названия читают `format_state`, судья логики и аудит (общий `_flag_line`: `key=value (имя)` — ключ остаётся), записи дневника (`journal.notable_diff`) и «ружья Чехова». Уже созданные миры чинит идемпотентное само-исцеление при старте сервера (`backend/flag_titles.repair_flag_titles`, вызывается в app.py вместе с п.9): подписывает ТОЛЬКО флаги без имени, имя для которых знает сюжет-источник мира (`_theme_snapshot.id` → `plots.get_plot`), значения не трогает, имя мастера не перетирает; живой прогон: 32 флага по всем мирам, мир №103 → «Пробуждение случилось» / «Сердце Фрактала запечатано». Системные сюжеты тоже подписаны (правки данных, не логики). Регрессии (tests/test_session40_p12_flags.py): директива с title и без, перезапись/сохранение прежнего имени, потолок длины, `format_state`/судья/дневник/`apply_plot_start` (обе формы), само-исцеление (в т.ч. идемпотентность и «не перетирать мастера»), белый список патча, НАСТОЯЩИЙ `flagLabel` на Node, правило 17 и описание `game_engine` обязаны знать `title` (иначе модель его не даст). Цена/страховка: правило 17 при удлиннении упёрлось в бюджет окна (`test_build_messages_never_overflows_window`) — формулировка сжата, как в п.7/п.8.
- **Сессия 40, п.14b/15/16 (мир «Новый мир»): часы идут, тики видно, «Механика применена.» — крайний случай.** Три хвоста того же класса (залипшее состояние / невидимая работа движка / служебная строка вместо сцены). **п.14b — «природа» (время суток) тоже залипало**: сюжет даёт `time: "вечер"`, меняет его только директива `time`, и за 40+ ходов мира №103 вечер не сменился ни разу — расписания NPC («ночью таверна закрыта») и «Влияние среды: ночь» противоречили тексту. Добавлен `mechanics.tick_time(setting, every)`: движок сам ведёт **только календарь** по циклу `TIME_CYCLE` утро→день→вечер→ночь (каждые `AUTO_TIME_EVERY=4` хода, сутки ≈ 16 ходов), с системкой «🕐 Часы мира: вечер → ночь»; новый мир заякоривается молча (`_time_anchor_turn`), чтобы не теряет вечер на первом ходу; свои/авторские значения времени («три склянки») не трогаются (закон 1), **погоду авто-часы НЕ меняют** (закон 3 — «рассеять туман» решает мастер, ему как раз ⚠-подсказка из п.14). Выключается глобально `AUTO_TIME_ENABLED`, тикает в том же блоке начала хода, что effects/needs/timers (и так же не задваивается при перегенерации). ⚠-строка в `format_state` теперь про **погоду** (`_weather_last_turn`, общий `_env_last_turn` — фолбэк), время из неё убрано: часы ходят сами. **п.15 — «эффекты не применяются / нет уведомления»**: на живом ходу (`⏳` в БД — 0 во всех мирах) тик был включён, но сам ход играли ДО правки п.1 (MP-канал), поэтому системка убыли не печаталась; попутно `tick_effects` теперь сообщает и **обратный отсчёт** временного эффекта («⏳ «Дурман» осталось 2 ходов») — раньше системка выходила только при уроне/лечении, и «эффект тикает молча» было неотличимо от «эффект не работает» (закон 2: только показ; длительность и снятие — как были, на движке). **п.16 — рассказчик сказал только «Механика применена.»**: в tools-режиме модель иногда отдаёт ОДИН `tool_call` без прозы; фолбэк `_narrate_mech_outcome` был, но на пустой ответ модели возвращал "" — и игрок получал служебную строку. Теперь: до `MECH_NARRATE_RETRIES=2` повторов (t чуть выше, чем в прошлый раз), срез `<<ENGINE>>`/game_engine из дописанного текста (механика уже применена — задвоения быть не должно, ловит тест), а если и повторы молчат — **детерминированный пересказ хода по системкам** (`_mech_fallback_prose`: «Ты продолжаешь: Переход: Тропа к Форпосту; …») и только при полном нечего-сказать остаётся «Механика применена.»; все ветки пишутся в лог (правило 14). Ключи (`AUTO_TIME_ENABLED`/`AUTO_TIME_EVERY`/`MECH_NARRATE_RETRIES`) — в `.env.example`, схеме `AdminIn`, `/api/admin/settings` (агент-секция), в UI админки и в `ALL_ADMIN_KEYS`; режим «Мастер» (`state/patch`) при правке `time`/`weather` обнуляет счётчики и пересаживает якорь авто-часов (иначе мастерскую «ночь» тут же сменило бы «утро»). Регрессии (tests/test_session40_p14_fog.py, всего 23): цикл часов и переход ночь→утро, молчаливый якорь на свежем мире, шаг/`every=0`/невалидный шаг, «не лезть в авторское время» и «погоду не трогать», патч мастера пересаживает якорь, ⚠-строка по `_weather_last_turn` (и фолбэк на `_env_last_turn`), отсчёт временного эффекта и «постоянный эффект молчит», пустой ответ модели → повтор → пересказ (мок `llm.complete`), срез утёкшего `<<ENGINE>>` из дописанной прозы. Проверялось живьём: 628 pytest + `scripts/check_frontend.py`.

- **Сессия 40, п.14 (мир «Новый мир»): текст перестал быть «сплошным туманом».** Симптом: почти каждый ответ рассказчика начинается с «Туман …» и держит его до конца (в мире №103 — 7…9 упоминаний на 11 ответов), читать тяжело. Стиль/читаемость при этом были в порядке после п.4 (CSS), промпт рассказчика тоже нормальный — причина лежала в СОСТОЯНИИ: сюжет `raskolotye-nebesa` даёт `starting_state.start_weather = "туман"`, `apply_plot_start` ставит `setting["weather"]` ОДИН раз, а дальше погоду двигает только директива `weather` (`mechanics.WorldHandler`) — за 40 ходов мастер не дал её НИ РАЗУ (и `time` тоже). При этом `format_state` каждый ход совал в промпт «Погода: туман» + «Влияние среды: −2 к видимости…» (правило 24 прямо велит учитывать это в описаниях), а стартовое вступление мира — «Туман стелется над руинами». Модель честно описывала туман каждый ход: строка состояния работала якорем, и повтор наложился на повтор (тот же класс дефекта, что п.8: дубль из-за залипшего значения, а не из-за стиля). Движок погоду НЕ меняет (закон 3 — решение за мастером; авто-«рассеял туман» по счётчику запрещено и ловится тестом). Сделано: `WorldHandler` при `time`/`weather` пишет `setting["_env_last_turn"] = _player_turns`, а `format_state`, если среда не двигалась ≥ 3 ходов, добавляет строку «⚠ Среда не менялась N ходов — не тащи её через весь текст: сдвинь weather/time или пиши сцену без погоды». ПРАВИЛО 24 при этом не удлиняли: тест бюджета окна (`test_build_messages_never_overflows_window`) снова покраснел (в мире №103 контекст 262k, но локальная модель/окно считаются по нему, а любая лишняя фраза в промпте каждого хода — это то, за чем тест и следит), поэтому сигнал живёт в строке СОСТОЯНИЯ, которую модель читает и так каждый ход (тот же приём, что «⚠возможен дубль имени» в п.8). Правка погоды/времени в режиме «Мастер» (`state/patch`) гасит предупреждение тем же ходом (`_env_last_turn`, ключ добавлен в белый список патча). Регрессии (tests/test_session40_p14_fog.py, 9): директивы `time`/`weather` запоминают ход; без директивы `_env_last_turn` НЕ появляется и погода не меняется сама (закон 3); предупреждение при возрасте ≥ 3 и тишина при < 3, на свежем мире и на мусорных значениях счётчика; сигнал попадает в реальный промпт и не ломает бюджет окна. Заодно отмечено (НЕ правилось, кандидат в следующую сессию): у `setting["time"]` та же природа — ночь/вечер тоже залипают навсегда.

- **Сессия 40, п.13 (мир «Новый мир»): игрок перестал числиться NPC.** Симптом: в «Состоянии» и сайдбаре раздела «🗣 NPC» рядом с «Торговец Орин» стоял `player = Игрок`, а его «настроение» было выдержкой из биографии героя («Проглотил кристалл Порядка… Требуется стабилизация узора»); ту же карточку `npc/player` с ключом `player` держала вкладка «Карточки», карта мира рисовала игрока «живым NPC», а быстрые подсказки предлагали «Поговорить с Игрок». Причина НЕ в одной точке: рассказчик вёл игрока директивой `npc_set`, фоновый архивариус заводил под это карточку, а синхронизация карточек (`memory.update_entity_cards`) переливала её обратно в `setting.npc` — т.е. дубль возвращался даже после ручной чистки. Состояние героя живёт в `setting.player`: раздел NPC — про ОКРУЖЕНИЕ. Код при этом не решает за мастера (закон 3): он только не даёт смешивать сущности и объясняет отказ. Сделано: `mechanics.is_player_npc(key, npc)` (по ключу `player/pc/protagonist/…` или по имени «Игрок/Герой»; «Игрок в маске» и `player_two` НЕ ловятся) — один предикат на всех. `NpcHandler`: `npc_set`/`npc_kill` по игроку → отказ + системка («⚠ Игрок — не NPC: … player / game_over»), остальные персонажи как раньше; там же у NPC появилось поле `location` (где стоит) — без него «рядом» было только на словах модели. Показ: `format_state` не даёт игрока в строке NPC (заголовок теперь «NPC (окружение, не ты сам)»), рядом стоящие — первыми, далёкий помечен «не рядом: <имя локации>», из «🗝 Знаний и тайн» игрок тоже снят; `audit_messages`/`judge_messages` не считают его фактом-истиной «жив/мёртв» (портрет игрока у них и так есть); `graph.build_graph` не заводит узла `npc:player` (в `/map` и «Живые NPC»); фронтенд фильтрует сайдбар и подсказки (тот же критерий, проверен НАСТОЯЩИМ JS на Node). Архивариус: карточку `npc/player` не пишет (страховка и на синхронизации в `setting` — иначе дубль вернулся бы на следующем ходу) и ему это сказано в промпте. Уже заведённые миры чинит идемпотентное само-исцеление при старте (`memory.heal_player_as_npc` в том же цикле app.py, что п.9/п.12): убирает дубль из `setting.npc` и карточку из БД, значения игрока не трогает, и пересобирает карту (`graph.sync_from_setting` — узел `npc:Игрок` иначе висел бы на «Карте» до первого хода); векторы удалённой карточки ставятся в `memory.PENDING_VECTOR_KEYS` и вычищаются из Chroma в `_lifespan` (на импорте цикла событий нет — иначе «память помнила бы удалённое», правило 14). Регрессии (tests/test_session40_p13_player_npc.py, 10): предикат (положительные/отрицательные), директивы (отказ + Ordinary NPC жив, `location`, `npc_kill`), `format_state` (нет игрока, есть пометка «не рядом», нет протёкшей биографии), судья/аудит фактами, живой вызов `update_entity_cards` с мок-LLM (карточка `player` не создана, в `setting` не попала — тест краснеет, если убрать страховку), `heal_player_as_npc` (2 → 0, идемпотентность, pending-векторы), граф, два прогона реального JS на Node. Цена/страховка: правило 3 промпта дополнено ровно фразой «NPC — не ты сам.» — длиннее не влезло (`test_build_messages_never_overflows_window`), как в п.7/п.8/п.12; поле `location` добавлено в описания `npc_set` в tools-режиме и в audit-промпте.

## 🔧 Известные фиксы: отчёты сессий 22 и 30
*(из `AGENT.md`, «СОГЛАШЕНИЯ ПО КОДУ → Известные фиксы»)*

  - **Карточки квестов копили desc в bio и показывали латинский id (сессия 30)**: `ensure_knowledge_cards` каждый ход вызывал `_upsert_knowledge(bio_add=desc квеста)` → `db.upsert_entity` дописывал desc в `bio` без дедупликации (карточка превращалась в простыню из N повторов); плюс `name` карточки = латинский `entity_key` (faction_alignment), а не человекочитаемый title. Исправлено: (а) `db._upsert_entity` не дописывает `bio_add`, если его начало (первые 80 симв.) уже есть в `bio`; (б) `ensure_knowledge_cards` для квестов передаёт `name=title`, `summary=desc`, `bio_add=None`. Регрессия: `tests/test_integration.py::test_quest_cards_no_bio_duplication_and_human_name`. Проверено вживую: 3 хода → name «Сделка с демонами», summary один раз, bio пустой.
  - **Пустой персонаж при создании мира (по SESSION_ISSUES .md, мир 35)**: `generate_character` обрывался по лимиту `max_tokens=1100` (полный персонаж часто >1100 токенов) → `json.loads` падал → `return None` → фолбэк `generate_identity` писал сырой зацеп и не заполнял расу/класс/статы/навыки/уровень. Исправлено: `generate_character` поднят лимит до 2400 + принимает `lore` (лор мира в промпт для согласованности с «библией» вселенной); `apply_character` мапит англ. ключи статов через `_STAT_LU` (strength/agility/endurance/intellect/wisdom/charisma/luck → русские ключи) — защита от англ. вывода LLM; `create_world` собирает/передаёт lore и логирует `None` вместо тихого `pass`, в фолбэке подставляет имя из зацепа. Проверено вживую (создание мира «Ключи Пангеи» с зацепом даёт заполненный персонаж: имя/раса/класс/профессия/статы не 10/уровень/навыки/инвентарь). Регрессия: `tests/test_mechanics.py::test_apply_character_maps_english_stat_keys`, `::test_apply_character_russian_keys_and_role_fill`.
  - **Локальная озвучка обрывалась на середине текста (Piper/Kokoro, сессия 22)**: `_synth_piper`/`_synth_kokoro` склеивали куски текста `out += _wav_bytes(...)` — то есть просто склеивали несколько WAV-файлов байтово. У каждого куска свой WAV-заголовок, поэтому плеер по заголовку первого куска читал ТОЛЬКО первую часть и «заканчивал на середине текста, как будто у модели мало выходных токенов». Исправлено: куски складываются в общий PCM-поток (`_chain_samples`) и пишется ОДИН WAV с полным аудио (6760 → весь). Проверено: текст 6480 симв. раньше давал ~64 с (≈1 кусок), теперь ~353 с (весь). Edge не затрагивался (поток без заголовков).
  - **Наррация терялась, когда модель возвращала только механику (сессия 22)**: если LLM отвечал одним `tool_call`/`<<ENGINE>>` без прозы, ход писал бессмысленное «Механика применена.» (игрок не видел, что произошло). `routers/core.py` добавили `_narrate_mech_outcome`: при пустом `final_text` компактный LLM-проход (системный промпт + применённая механика из `sys_msgs`) генерирует связный текст 1–3 предложения; при сбое остаётся «Механика применена». Регрессия: pytest 117 passed + живой ход вернул нормальный нарратив.
  - **Судья не видел инвентарь → не ловил «предмет, которого нет» (гранаты/реактор/бомба) (сессия 22)**: `judge_messages` не передавал `player.inventory`, поэтому рассказчик мог беспрепятственно «доставать из воздуха» отсутствующие у героя предметы. Добавлена строка `ИНВЕНТАРЬ: …` (имена предметов) в факты судьи + явное правило в system-промпт про «достаёт/использует предмет, которого НЕТ в инвентаре = грубое противоречие». Регресс: `test_judge_messages_includes_inventory`.
  - **Сообщения судьи/искажения обрывались на полуслове (сессия 22)**: `issue[:200]`/`twist[:400]` рубили текст в середине слова (в UI виделось «…вторая граната опи»). Трукация заменена на `_cut_words` (по границе последнего пробела + многоточие). Регресс: `test_cut_words_no_midword_truncation`.

## 🔧 Сессии 6–10 (UX, карта, озвучка, атомарность хода)
*(из `AGENT.md`, «СОГЛАШЕНИЯ ПО КОДУ → Известные фиксы»)*

  - **TTS-озвучка (сессия 8, по 1.txt)**: модуль `backend/tts.py` (Piper локальный через sherpa-onnx — русские голоса, Kokoro, Edge облачный); фоновый синтез после хода (не блокирует ответ), статусы события 0/1/2/−1, кэш по хешу текста+голоса (таблица `tts_cache`, файлы `data/tts/cache/`); per-world `tts_settings` (вкладка Настройки: вкл/провайдер/голос/скорость/авто-плей, проверка и скачивание голосов), глобально → админка; кнопки 🔊/⟳/⚠ у ответов рассказчика; `sherpa-onnx + edge-tts` установлены в системный python, русский голос `ru_RU-ruslan-medium` скачан (data/tts/voices). Скрипт установки — `scripts/setup_tts.bat`.
  - **Новые фичи (сессия 7)**: вкладка «Карта» (рёбра `locations[].connections`, фиксируются в `move`/`location_add`, SVG-расклад BFS от текущей локации, клик по соседнему узлу = действие «идти»); **динамические события мира** (`_maybe_dynamic_event` в app.py, фоново, не блокирует ход): шанс события растёт каждый ход — `p = (ходы с прошлого события)/EVENT_EVERY_TURNS` (`narrator.event_chance`, никогда не гарантирован — потолок 0.5); таймер по реальному времени убран — события срабатывают только по ходам игрока (`.env: DYNAMIC_EVENTS_ENABLED / EVENT_EVERY_TURNS`), счётчик `setting._player_turns`; LLM-генератор `narrator.generate_dynamic_event` → системное сообщение + директивы применяются `apply_directives`; событие догоняется живым чатом через `GET /events?since=` (poll в frontend). **Варианты действий от ИИ** — `narrator.generate_suggestions` (3–4 ситуационных «бытовых» действия от первого лица, приходят в `result.suggestions`, таймаут 12 с) → кнопки под чатом (`renderSuggestionBar`: ИИ-варианты, фолбэк — бытовые заготовки от состояния); клик = то же действие, что и ручной ввод.
  - **Сессия 7.1**: карта мира — зум/панорама (колесо мыши, перетаскивание, кнопки +/−/⤢, двойной клик), кольца без жёсткой крышки + автоподгон viewBox под весь граф (много локаций не слипаются), подписи 17px с обводкой-гало; эффекты: id-подобные имена (chill_resonance) показываются читабельно (Chill Resonance) + desc в системных сообщениях, `format_state` и UI (правило 20 для рассказчика: человекочитаемые имена и desc в effect_add).
  - **Сессия 9 (UX-фиксы)**: «Быстрые действия» от ИИ сохраняются в localStorage per-world (`textgame.suggestions.<worldId>`) и восстанавливаются при перезагрузке страницы (раньше сбрасывались на фолбэк); при открытии мира/загрузке слота лог сразу листается в конец (к последнему ответу); убраны случайные события по таймеру реального времени (`EVENT_EVERY_SECONDS` удалён из config/app — события только по ходам игрока, `_player_turns`); **гарантия события у порога убрана** — `narrator.event_chance` теперь потолок 0.5 (только шанс, без «расписания»); пофикшен Edge TTS «Invalid rate '0%'» — `tts._synth_edge` нормирует rate со знаком (`+0%`, `-10%`), фронт шлёт `ttsSignedRate()`; голоса Edge: убран нерабочий `ru-RU-DariyaNeural`, добавлены проверенные en-US/en-GB нейроголоса (Aria/Christopher/Eric/Michelle/Ana/Sonia/Ryan), Edge — рекомендованный провайдер в UI/README; **очистка текста перед озвучкой** (`tts._clean_tts_text`): убирает `*`-разметку, эмодзи и «xN/×N»-количества (чтобы не читалось «умножить на два»), применяется до хеша кэша; **выпадающий список «Голос»** в настройках мира — настоящий `<select>` (обновляется при смене провайдера, показывает все варианты даже при выбранном значении) + пункт «✍️ Свой голос…» с полем ввода (раньше сломанный datalist); **фикс скролла сайдбара**: `.tab-panels` получил `min-height: 0` (+`flex-shrink: 0` у шапки и вкладок), игровой экран `#screen-game` — жёсткую высоту `100vh` с `overflow: hidden`; вкладки `.tabs` — горизонтальный скролл (активная вкладка подскролливается в видимую зону через `scrollIntoView` при клике) — «Настройки» всегда доступна, переноса на вторую строку нет; **кнопка «Админка»** скрывается при открытии мира (`openWorld`) и возвращается в меню (`goMenu`); **фон** — `background-attachment: fixed` + `no-repeat` (градиент не дублируется и не режется при прокрутке); **настройки голоса в админке** — как в мире: select голосов + «✍️ Свой голос…» (`admin.html`, `fillAdminVoiceOptions`), кнопки скачивания Piper/Kokoro показываются только когда нужны; **кнопка «Скачать голос» в мире** — видна только при провайдере piper с нескачанным голосом или kokoro без модели (`updateTtsVoiceToolbar`); **смена провайдера** больше не подставляет «свой» голос с прошлого провайдера — выбор сохраняется только если голос есть в списке нового (`keep`-логика), иначе «По умолчанию»; **перечисление голосов текстом под настройками удалено** — голоса теперь только в выпадающем списке.
  - **Сессия 10 (надёжность — по 1.txt)**: **атомарная запись хода** — `db.transaction()` (db.py): события ответа + системные сообщения + `update_world(setting)` + карточки знаний пишутся одной транзакцией SQLite (`BEGIN IMMEDIATE` → COMMIT/ROLLBACK, `_tx_depth`; `_lock` стал `RLock` — реентерабельный), чтобы при сбое не осталось «событий без состояния»; **RAG больше не молчит** — логирование (`textgame` logger, Warning) в `retrieve_memory`/`index_exchange`/`index_summary` и фоновых задачах `_background_memory`/`_background_cards`/`_maybe_dynamic_event` (игра живёт, но ошибка видна в логе); **лимит длины действия** — `MAX_ACTION_CHARS` (.env, дефолт 640 ≈ 200 токенов): `_ensure_action_len` в `action`/`action_stream` (400 с понятным сообщением «Разбей на несколько шагов»); **фикс числовых id в директивах** — `normalize_directives` приводит `id` к строке для `enemy_add/enemy_apply/quest/npc_set/location_add/location_update/flag` (раньше LLM мог вернуть `id: 7` → `.strip()` краш хода); **регрессионный тест** — `scripts/test_directives.py` (чистый Python, без pytest): 35+ мусорных директив, снапшот-тик при перегенерации (не задваивается), roll_expr/roll_outcome, атомарность+роллбэк `db.transaction()` — подтверждено, что снапшот сохраняется **до** тика эффектов (строки 280→286 app.py) — анализ (п. «перегенерация сбрасывает snapshot») закрыт ещё сессией 5, тест закрепляет поведение.

## 🧪 Тестовый набор: состав и история прироста
*(из `AGENT.md`, «СОСТОЯНИЕ И ИСТОРИЯ → Контроль качества»: по сессиям и по файлам)*

  в доке было написано 495 — не соврал, но и не совпало; +38 в сессии 39, +15 в сессии 40,
  +14 — баг-хант мира «Новый мир» п.8/9/10, +7 — п.11, +15 — п.12, +10 — п.13, +9 — п.14,
  +14 — п.14b/15/16 (в файле 23 теста), +14 — XSS/A2 (сессия 42), +4 — дубль действия в
  промпте/A3 (сессия 43), +3 — судья по устаревшему состоянию/A4 (сессия 44), +12 — откат
  под барьером/A5 (сессия 45), +15 — тихие ошибки/A6 (сессия 46), +6 — дневник в SQL/A7
  (сессия 47), +9 — потолок страницы /history/A8 (сессия 48), +20 — границы настроек/A9
  (сессия 49), +4 — заметка дневника через владельца/A10 (сессия 50), +8 — событие
  `rewound` до вкладок/A11 (сессия 51), +10 — честный статус модели/A12 (сессия 52),
  +7 — само-исцеление на старте, а не на импорте/A13 (сессия 53, в файле 9 тестов),
  +2 — рабочий шаг mypy в CI/B1 (сессия 54),
  +17 и +1 — rate-limit на «дорогих» роутах, localhost для админки, отказ launcher'а
  выходить в сеть без подтверждения/B2 (сессия 55),
  +15 — границы размеров пользовательских текстов/B3 (сессия 56),
  +7 — правда о мусоре в рабочем дереве/B4 (сессия 57),
  +11 — мёртвый/лишний конфиг: GAME_HOST/GAME_PORT, осколок RERANK_TOP_N/B5 (сессия 58),
  +11 — числа в трёх документах сводит механизм, а не сессия руками: реестр
  `scripts/doc_figures.py` + `--sync` и `test_docs_figures_agree`/C1 (сессия 59):
  `test_session38_bugfixes.py` (баги игрока/API/конфига/доков),
  `test_session40_bugfixes.py` (баг-хант мира «Новый мир»: стиль рассказчика + предупреждение
  в README; п.8 — дубль эффекта из-за имени-синонима),
  `test_session40_p9_cards.py` (п.9 — человекочитаемые имена карточек) и
  `test_session40_p10_journal.py` (п.10 — переход из дневника к ходу, реальный JS на Node),
  `test_session40_p11_stats.py` (п.11 — человекочитаемые ярлыки статистики пути: `/stats`,
  картка персонажа, реальный JS на Node и сверка словарей фронта/бэкенда),
  `test_session40_p12_flags.py` (п.12 — `flag_titles`: директива/сюжет/судья/дневник/UI,
  само-исцеление названий в уже созданных мирах, реальный JS на Node),
  `test_session40_p13_player_npc.py` (п.13 — игрок не NPC: директивы/показ/судья/карточки/
  граф/само-исцеление, реальный JS сайдбара и подсказок на Node),
  `test_session40_p14_fog.py` (п.14/14b/15/16 — залипшая погода и часы, видимый отсчёт
  эффектов, «Механика применена.» как крайний случай: метки `_env_last_turn`/
  `_weather_last_turn`/`_time_anchor_turn`, `tick_time`, `_mech_fallback_prose`, закон 3
  «код не меняет погоду» и бюджет окна промпта),
  `test_session42_xss.py` (аудит 41, A2 — экранирование модельных текстов во фронте),
  `test_session43_prompt_dup.py` (аудит 41, A3 — действие игрока не дублируется в промпте:
  реальный контур роутеров + `build_messages`, счёт вхождений текста действия, ↻-ветка,
  юнит `_recent_block(exclude_ids=…)`),
  `test_session45_rewind_barrier.py` (аудит 41, A5 — перемотка/загрузка сохранения под барьером
  фона: реальные роутеры + два прохода в одном цикле событий, инвариант п.3Б — сброс реестра
  хода остался, отмена отката не оставляет залипшего барьера),
  `test_session46_quiet_errors.py` (аудит 41, A6 — правило 14 до конца: `db.close()` больше не
  глотает отказ закрытия, отказ фоновых агентов пишется ОДИН раз на (агент, мир) с traceback,
  док `logsetup` не обещает несуществующих хелперов, 429 видно в журнале; перехват логов —
  живым `logging.Handler`, не чтением исходников),
  `test_session47_journal_sql.py` (аудит 41, A7 — `/journal` и категории дневника: `limit`/`cat`
  доезжают до слоя запроса (шпион над `db.list_cards`), счётчик категорий не выбирает карточки,
  битая/пустая meta не роняет дневник и не пропадает из счётчика, живой GET /journal и
  `/journal note`/поиск; поведение, а не текст исходников — правило 19),
  `test_session48_history_page.py` (аудит 41, A8 — `GET /history?limit=0` больше не «весь лог»:
  потолок страницы и честный `truncated` в слое данных и в HTTP-ответе, роль-фильтр не портит
  флаг, настоящая `loadEarlier` на Node переживает новую форму ответа, а `LOG_PAGE_SIZE` фронта
  влезает в серверный потолок),
  `test_session49_settings_clamp.py` (аудит 41, A9 — «кривое» число больше не ломает все
  будущие ходы: реестр границ и его самосогласованность, кламп .env/админки/ротора журнала,
  roundtrip `POST /settings` с `gen_limits`, эффективные настройки в `GET /worlds/{id}`,
  кривой gen_settings из старого дампа не доезжает до модели и не роняет бюджеты, мусор в
  админке → 400, пустая строка → по-прежнему сброс к .env, подрезка видна в журнале и не
  зашлась лог-штормом (правило 14 + A6: `log_once` на «ключ + значение»); поведение, а не
  тексты исходников — инвариант 19),
  `test_session50_note_owner.py` (аудит 41, A10 — `/journal note` больше не дублирует
  `journal.add_player_note`: шпион поверх настоящего владельца ловит делегирование, карточка
  из-под команды сверяется с карточкой прямого вызова (ключ/`cat`/`icon`/`seq`), лимит 400 и
  дедуп едины для обоих путей, пустая заметка не создаёт карточку и не вранёт «Записано»),
  `test_session51_rewound_event.py` (аудит 41, A11 — обещание «все вкладки получают rewound»
  исполнено: шина реально получает тип `rewound` ПОСЛЕ откатанных записей в БД, строка отката
  несёт `meta.rewound` (и ⏪, и 💾 hide), отказ 400/409 не рассылает ничего, `/events` несёт
  `latest_seq` для вкладок без SSE; настоящие `pollEvents`/`applyRewound`/`reloadWorld`/
  `startLiveBus`/`loadSaveSlot` на Node — перезагрузка вместо дорисовки к удалённому, «замок»
  `state.reloading` не даёт грузить мир дважды и не залипает на отказе; поведение, а не тексты
  исходников — инвариант 19, протокол типов сторожит `check_frontend.bus_types_check`),
  `test_session52_llm_status.py` (аудит 41, A12 — «жива ли модель»: ОДИН критерий на весь
  проект. Мок `_get_client()`: 401 → `up=True` + `needs_key=True` (ход работает, а плашка
  врала), refused/таймаут → `False`, 5xx → `False`, пустой `base_url` → без запроса;
  заголовок `Authorization` реально отправляется и не светится в ответе `/api/system/status`
  (правило 3); `check_available` сверяется с `probe` — второго критерия не завести снова),
  `test_session53_startup_heal.py` (аудит 41, A13 — стартовое само-исцеление переехало с импорта
  модуля в `_lifespan`: импорт `backend.app` в подпроцессе на свежей базе НЕ сеет рассказчиков
  (деградация: возврат сида на импорт красит тест), `self_heal()` возвращает счётчики и
  идемпотентен, реально лечит ранги/флаги/игрока-как-NPC/сирот, lifespan его запускает,
  отказ пишется в журнал (правило 14), а `GET /api/narrators` и создание мира на пустой
  таблице досеивают пресеты сами — гонка первых миллисекунд старта не оставляет мир без
  рассказчика),
  `test_session54_ci_mypy.py` (аудит 41, B1 — CI-шаг mypy: команда из `.github/workflows/ci.yml`
  достаётся из YAML и ЗАПУСКАЕТСЯ, требуя код 0 (regression на откат к `mypy backend`);
  вторая проверка честна к себе — positional форма обязана падать; mypy через importorskip (18)),
  `test_session55_ratelimit_routes.py` (аудит 41, B2 — реестр «дорогих» роутов: у каждого
  ищется guard по РЕАЛЬНЫМ зависимостям маршрута FastAPI (инвариант 19: поведение приложения,
  а не тексты роутеров), обратно — в приложении не может остаться scope'а вне `SCOPES`;
  щедрые и конечные лимиты реестра, неизвестный scope ≠ «без лимита», отказ = 429 с ключом в
  журнале (A6), выключенный лимитер не режет ничего, чужому адресу лимиты жёстче в
  `REMOTE_FACTOR` раз (и поведением, и арифметикой), `/api/admin/settings` — 403 с чужого
  адреса и 200 с localhost, `ADMIN_ALLOW_LAN` в `hidden_admin_keys` (админка не разблокирует
  себя сама), `RATE_LIMIT_ENABLED` применяется стартом (`_lifespan`), а не висит обещанием),
  `test_session56_text_limits.py` (аудит 41, B3 — ни одного текстового поля без границы:
  гигантский текст отклоняется 422 на narrator/plot/lore/создание мира/divine/карточки/слоты
  ДО записи в БД и до обращения к модели; жёсткий потолок действия шире мягкого лимита админки
  (400, а не 422), иначе настройка `max_action_chars` потеряла бы смысл; `detail` — текст, а не
  pydantic-объекты, и отказ виден в журнале (правило 14); границы сверяются с фактическими
  текстами пресетов/сюжетов — «впритык» красит тест; персона, уже лежащая в БД, подрезается
  бюджетом промпта и не задевает ни один штатный рассказчик (деградации: снятие `max_length`
  и возврат `(persona or "")` в `build_system_prompt`); лор при переборе окна урезается ПЕРВЫМ
  — он в жертвенной секции; поведение, а не тексты исходников — инвариант 19),
  `test_session57_worktree_trash.py` (аудит 41, B4 — док больше не вран про рабочее дерево:
  канон мусора правила 15 покрыт `.gitignore` (деградация: вырезать накрывающие путь строки —
  `check-ignore` обязан отказать; и целый исходник `backend/app.py` игнорируемым не считается),
  реально лежащие `nul`/`server_restart.log` проваливаются В GIT не могут (индекс пуст по ним),
  живые разделы AGENT.md не содержат «мусор удалён» без оговорок «может появляться локально» /
  «удалять с разрешения владельца» (деградация: возврат прежней формулировки красит 3 теста),
  а названные источники мусора (`>nul` в `.bat`) проверяются по настоящим файлам — инвариант 19,
  сам тест НИ ОДНОГО файла не удаляет: это решение владельца),
  `test_session59_docs_figures.py` (аудит 41, C1 — повторяющиеся числа в README/AGENT/ROADMAP
  больше НЕ сводятся руками: реестр `scripts/doc_figures.py` знает, что мерить
  (`pytest --collect-only`, `len(NARRATOR_PRESETS)`, файлы `plots/system|user`, `len(GENRE_HINTS)`)
  и где это заявлено; заявления обязаны сходиться с реальностью, CLI-проверка выходить в 0
  (она же шаг CI), каждое заявление находиться РОВНО один раз и ни одно — не залезать в
  исторические сводки (деградации: подменное число/падеж/потеря заявления красят; `--sync`
  правит ровно строку с числом, идемпотентен и сохраняет переносы файла; сверка на копиях
  в tmp_path — живые доки тест НЕ пишет),
  `test_session60_openapi_documented.py` (аудит 41, C2 — README больше не врёт про API дважды:
  числа путей/операций `app.openapi()` (50/64) стоят под реестром `doc_figures`, а НЕЧЕСТНОСТЬ
  перечисления ловится по схеме приложения: каждый путь схемы назван в таблице README «🗂 API»,
  в таблицах README/AGENT не осталось путей, которых в приложении уже нет, и ссылка README ведёт
  на ЖИВОЙ `test_openapi_paths_documented` (деградации: удаление строки таблицы и придуманный
  путь красят пробы; живые доки тест не пишет — портится текст в памяти),
  `test_session38_layers.py` (инварианты слоёв: A9, D6–D10),
  `test_session38_tails.py` (28 тестов закрытия хвостов 38: E1-делегат, E6-адаптивность,
  E7-динамические id, D5-типы, D11-bat-чекер, D15-схема, B1-мусор вне git — с
  degradation-пробами «вернули плохое → поймали») и
  `test_start_bat_harness.py` (11 тестов: `start_game.bat` ИСПОЛНЯЕТСЯ `cmd /c` в песочнице с
  подменёнными curl/netstat/start_chroma.bat и фейковым модулем uvicorn — проверяются ветки
  запуска, а не текст; именно он вскрыл поломку launcher'а — инвариант 15а). На Linux харнесс
  skip'ится, там bat прикрывает `scripts/check_start_bat.py` (он в CI).

### Сводка сессии 38 (сплошной аудит)

Проверено всё: статический анализ, тесты, «фаззинг» API на несуществующем мире, чтение ядра
хода/движка/памяти/слоя БД/фронта/`.bat`/доков, анализ боевой БД (только чтение), история git.

**Из 58 найденных пунктов закрыто 50. Восемь остались открытыми — они перенесены в ROADMAP
(раздел «🔴 Хвосты сессии 38») и НЕ отмечены здесь как сделанные: E1 (перевод `onclick` на
data-атрибуты), E6 (мобильная адаптивность), E7 (явный whitelist динамических id в
check_frontend), B1/B3 (удаление `.env.bak*` и мусора рабочей копии — нужно твоё разрешение;
в git они никогда не были), D5 (остатки mypy в старых модулях — сигнальные разобраны),
D15 (строгая JSON-схема в check_plot — реестр битых файлов и плашка в UI сделаны).**

> **Состояние хвостов на сегодня (сессия 39): закрыты все восемь.** E1, E6 (минимум по
> формулировке пункта), E7, D5 (mypy чист при `check_untyped_defs = true`), D11 (и структура
> `scripts/check_start_bat.py`, и живой `cmd /c`-харнесс `tests/test_start_bat_harness.py`),
> D13-хвост (`setup_tts.bat`), D15-хвост (строгая `PLOT_SCHEMA`), B1/B3 (мусор удалён по
> решению владельца). Харнесс D11 вскрыл живую поломку launcher'а: из-за неэкранированной
> скобки в `echo` внутри `if (...)` `start_game.bat` не запускал сервер НИКОГДА (жило с
> сессии 33) — исправлено в 6 местах двух bat, инвариант 15а + двойная страховка (поведение
> и структурный чекер для Linux-CI). Открытым осознанно осталось одно: живой авторский сюжет
> в `plots/user/` (нужен твой файл, не мой код). Подробности — ROADMAP, секции «Сводка
> сессии 39» и «🔴 Хвосты сессии 38»; проверки — `tests/test_session38_tails.py` и
> `tests/test_start_bat_harness.py`.

Файл `ISSUES_AUDIT_SESSION38.md` (доказательства по пунктам) удалён по указанию владельца
после переноса выводов: свёрнутый разбор — ниже в этом разделе, хронология сессий 4–37 —
в `AGENT_ARCHIVE.md`, комментарии к пяти коммитам сессии 38 — в `git log`.

- **Реальные баги (A1–A19)**: мертвые ружья Чехова (A1), потерянная системка снятия эффекта
  зоны (A2), **утечка живых api_key в `/api/worlds/{id}` и `POST .../providers`** (A3), 500 и
  «молчаливые 200» вместо 404 в 19 местах + фидбек/лор без скоупинга по миру (A4),
  `MAX_ACTION_CHARS` только до перезапуска (A5), кулдаун Провидения 0 вместо 3 на чистой
  установке (A6), теряющийся `event: done` и разъехавшиеся списки команд (A7), несвязанные
  таблицы при удалении мира + сироты в БД (A8), `_lock` не на всё тело транзакции (A9),
  сводки в логе игрока (A10), 500 при пустом вступлении (A11), неограниченные горячие
  выборки (A12–A14), гонка `↻`/Провидения (A15), метрика-«обманка» повторений (A16),
  декоративные `RERANK_*` (A17), невключаемый TTL кеша озвучки (A18), молчаливая потеря
  персоны рассказчика (A19).
- **Секреты/гигиена (B1–B5)**: скан CI приведён к правилу 15 (`sk-[A-Za-z0-9]{12,}`, по
  индексу + запрет `.env*` в `git ls-files`), `.env.example` покрыл все читаемые ключи,
  админка — обещанные настройки + единый реестр переопределяемых ключей (`overridable_env_keys`,
  `reset` с опечаткой → 400). Файлы-мусор и `.env.bak*` (в них 3 живых ключа в каждом)
  **НЕ удалены**: удаление файлов рабочей копии — решение владельца (AGENT.md: «не удалять
  без спроса»); в git они никогда не были (`.gitignore` кроет `.env.*`). Что сделано вместо
  удаления: `data/*.log`, `data/logs/`, `data/fastembed_test/` добавлены в `.gitignore`,
  журнал Chroma переведён в `data/logs/chroma.log` (старый `data/chroma.log` — 5.3 МБ без
  ротации); `scripts/_test_repair.py` и корневые `server*.log`/`nul` внесены в ROADMAP
  («🔴 Хвосты сессии 38») как кандидаты на перенос в `НЕ УДАЛЯТЬ ПАПКУ. ИДЕИ/` — на вынос
  и удаление тоже нужно твоё решение.
- **Документация vs код (C1–C11)**: нумерация правил промпта, пути `app.py`→`routers/*`,
  лимит вступления (2400, `character_generator`), «правила 26–28»→27–29, «44 пути»→49,
  полная таблица API (+тест), ревизия ROADMAP, опечатки (одна была в СИСТЕМНОМ промпте) +
  `scripts/check_typos.py`, канон трёх законов и канон слэш-командов, и этот перенос архива.
- **Мёртвый код/линтеры/переносимость (D1–D15)**: удалены 8 мёртвых хелперов и 20+ «на всякий
  случай» реэкспортов, F401 перестал быть глобальным, CI-линтеры починены (ruff по всем
  каталогам, mypy пакетом), `bg.spawn` вместо «огонь-и-забыл» (GC собирал задачи посреди
  игры), из `bg` удалены мёртвые `await_result`/2 приоритета, в `mechanics` свёрнуты
  мёртвые тернарники, `chroma add()` перестал плодить коллекции, `bus` — импорт из горячего
  except и протечка ключей, `.bat` переносимы и health-check сведён к коду ответа,
  соглашение `plots/*.js` документировано; битые файлы видны в UI (SCAN_ERRORS + /api/plots/errors),
  строгая JSON-схема полей — закрыта в сессии 39 (`check_plot.PLOT_SCHEMA` + рекурсивный
  `_tcheck`, без внешних зависимостей: битый по полям, но валидный JSON не проходит и уходит
  в ту же плашку `plots_errors`).
- **Фронтенд (E2, E3, E4, E5, E8)**: экранирование подстановок из пользовательских файлов и
  импортированных дампов (XSS-поверхность, E2), дедюп сообщений по `id` (раньше второе
  служебное терялось, E3), поллинг как фолбэк — не долбит API при живой ленте (E4), удаление
  мёртвого поля с ключами (E5), разбор мёртвых id в разметке (E8). **E1/E6/E7 закрыты в
  сессии 39**: 15 `onclick`-литералов вынесены в `data-click`/`data-arg` с одним делегатом на
  `document` (реестр `CLICK_ACTIONS`, тип аргумента — по `CLICK_NUMERIC`), `jsAttr()` удалена;
  whitelist динамических id стал словарём с обоснованием и проверяется в обе стороны;
  появилась минимальная адаптивность (4 `@media`, выдвижная панель мира).

---

## 📤 Из README.md (сессия 62, C4): отчёты сессий 34 и 36

> Перенесены дословно: публичный README обязан продавать игру, а не быть журналом фиксов.
> Живые правила от этих сессий разложены по `AGENT.md` (инварианты), поведение — по
> разделам README.

## ✨ Что добавилось (сессия 34)

- **⏪ Перемотка назад**: кнопка «⏪ Назад к ходу» (или `POST /api/worlds/{id}/rewind`) возвращает мир
  к началу любого хода: состояние, недавнюю память и дневник. Ходы после точки удаляются
  (или сокрываются при загрузке сохранения). Векторы отменённых ходов вычищаются из памяти —
  рассказчик не вспоминает того, чего уже не было. В середину чужого хода и под `↻` перемотка
  не лезет: мир занят — игрок получает 409 с просьбой подождать, а не тихо испорченное
  прохождение (аудит 41, A5).
- **📔 Дневник приключений** (вкладка «Дневник» / `/journal`): авто-хроника значимого —
  новые квесты и их итог, первые встречи, находки, смены роли, новые места, истёкшие сроки.
  Детерминированно (без LLM). Своя заметка — `/journal note <текст>`. Клик по записи — к событию в логе:
  ранние страницы чата догружаются сами, а если хода в логе уже нет (перемотка/`↻`) — открывается
  ближайший сохранившийся с внятным объяснением (сессия 40, п.10).
- **🧭 Мои средства** (`/risk <идея>` или кнопка): справка «чем персонаж может закрыть затею» —
  профильные статы, навыки, вещи, станции, репутация, вес рюкзака. Чистый форматировщик
  состояния, без LLM и без вердиктов — решает рассказчик.
- **🏹 Ружья Чехова**: в состоянии появляется «🏹 На горизонте» — то, что введено в мир и с тех пор
  не звучало. Хороший повод вернуть деталь в сюжет; но это не обязанность — список гаснет сам.
- **🗝 Тайны NPC**: у персонажей есть заметки мастера (`npc_set {notes: {знает, тайна, хочет, долг}}`) —
  что он знает и скрывает. Подача игроку — намёками, через проверки и репутацию (правило 35).
- **⚔️ Статусы на врагах и тактика**: `enemy_effect_add` (горение/страх) и `enemy_mark` (позиция/
  инициатива/цель) — подсказки для боя. Статусы **не тикают сами**: урон врагам ведёт рассказчик
  своим `enemy_apply` (иначе задвоился бы урон).
- **🏅 Итог квеста и срок**: `quest_success` / `quest_fail {id, reason, next}`; квесту можно дать
  дедлайн `quest {id, timer: {name, turns}}` — мир сам напомнит «срок вышел», а исход решает рассказчик.
- **🔴 Живой чат**: фоновые события (ход врага, подсказка мастера, истёкший таймер) приходят
  мгновенно через SSE; поллинг остаётся запасным. ⏪ Перемотка/💾 загрузка сохранения из другой
  вкладки шлёт по шине событие `rewound` (+ метку `meta.rewound` на служебной строке отката):
  все открытые вкладки перезагружают лог, а не дорисовывают его к удалённому прошлому (A11).
- **🧭 Компас и часы**: под чатом — соседние локации кнопками («идти →»), в шапке мира — время/погода/сезон.
- **📊 Качество ответов**: в метриках (`/api/metrics`) видны «ответы, упёршиеся в лимит» и
  «оборванные на полуслове», средняя оценка релевантности памяти; в плашке «🧠 Память» — сила совпадений.
- **⚙️ Новая наблюдаемость**: JSON-лог `data/logs/game.log` (контекст хода), ретраи временных сбоев
  LLM/Chroma/облака, очередь фоновых агентов с приоритетом (игрок всегда первый), ярусы промпта
  (правила о неиспользуемых подсистемах не едят контекст — замер: 5550 → 3873 ток.).

## 🔧 Стабильность (сессия 36 — исправлены 21 дефект аудита)

Поведение игры не изменилось, но закрыты риски, которые проявлялись на длинных
прохождениях, после рестарта сервера или при кривом выводе модели:

- **↻ Перегенерация после рестарта** больше не задваивает ход: события помечаются номером
  хода (`meta.turn`), и «замена последнего хода» работает даже с пустым in-memory реестром.
  На время перегенерации фоновые агенты (судья/мастер/боевой ИИ/события/видения) не пишут в
  мир, а перечитав состояние, отказываются от записи, если игрок успел сходить.
- **Механика больше не протекает в чат**: блок `<<ENGINE>>{…}` / `game_engine({…})`, вставленный
  моделью в середину абзаца, вырезается целиком, а проза до и после него остаётся. В живом
  стриме обрывок маркера («…идём к gam») тоже не показывается.
- **👁 Провидение нельзя фармить**: между воззваниями появился минимальный интервал
  (`DIVINE_COOLDOWN_TURNS=3`, сбрасывается в 0 в админке), Провидение по промпту чинит
  согласованность, а не выдаёт новую добычу, и каждая выдача ресурсов видна в логе.
  Первое воззвание в новом мире при этом доступно сразу (кулдаун не считает «ещё не звал»
  нулевым ходом).
- **Импорт мира из дампа** не даёт «сломанный мир»: недостающие поля персонажа достраиваются,
  битые строки журнала пропускаются с записью в лог, а не валят весь импорт.
- **Списки и карточки не разрастаются**: горячие пути (окно памяти, карточки сцены,
  обратная связь 👍/👎, пересборка памяти после импорта) читают БД ограниченным
  хвостом/страницами вместо всего лога; список миров считается одним SQL-запросом.
  Попутно найден и исправлен незаметный дефект: синхронизация карточек архивариуса в
  состояние мира писалась тремя блоками подряд, и поздние затирали ранние — квесты и NPC,
  заведённые рассказчиком через карточки, молча исчезали из «Состояния». Теперь правки
  копятся в одном объекте и сохраняются один раз.
- **Журнал метрик ротируется** (`METRICS_MAX_BYTES`, по умолчанию 4 МБ × 3 архива), а
  суммарные счётчики в `/api/metrics` переживают перезапуск (база берётся из журнала).
- **Тихие сбои стали видимыми** (правило 14): NameError в удалении карточки, недоступная
  таблица админки, «БД не отвечает» — всё пишется в лог; `db._run` больше не может висеть
  вечно (потолок ожидания + защита от само-блокировки).
- **Безопасность**: из дампа мира вырезаются ЛЮБЫЕ вложенные `api_key` (а не только в
  provider_settings), а тело ошибки провайдера в логах маскируется на случай эха ключа.
- **Мелочи UX**: кнопки «Быстрые действия» не залипают на устаревших ИИ-вариантах; поллинг
  озвучки не плодит таймеры; ключ сущности с апострофом (LLM вполне вернёт `don't`) не ломает
  модалку квеста/магазина/NPC; Edge-озвучка повторяется при 429 вместо ⚠; `start_game.bat`
  считает llama.cpp живым, если тот отвечает 401 (сервер с `--api-key`); при штатной остановке
  сервера соединение БД закрывается (на Windows файл больше не залочен).

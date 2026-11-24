# Запуск NFT Tracker с iPhone — бесплатно, без карты

Нужны: iPhone и Safari. Время зависит от настройки GitHub. Все секреты вы вводите только в формы GitHub, в чат их присылать не нужно.
Совет: в Safari для GitHub откройте «Вид сайта → Запросить настольный сайт» (значок «аA» в адресной строке).

## 1. Аккаунт и репозиторий
1. github.com → Sign up (e-mail и пароль, карта не нужна).
2. **+** → New repository → имя `nft-tracker`, **Public** (публичный нужен для бесплатных минут Actions), отметьте «Add a README file» → Create repository.

## 2. Загрузка кода (через бесплатный Codespaces)
Перед использованием проверьте текущие бесплатные лимиты Codespaces на GitHub: условия бесплатного предоставления и квоты могут меняться. Банковскую карту для этого проекта не добавляйте.
1. Сохраните ZIP проекта в «Файлы» на iPhone.
2. В репозитории: **Code → Codespaces → Create codespace on main**. Откроется редактор в браузере.
3. В боковой панели (Explorer) нажмите на пустое место → **Upload…** → выберите ZIP.
4. Откройте терминал (меню ☰ → Terminal → New Terminal) и выполните по одной строке:
```
unzip -o NFT-Tracker-v0.5.4-ChatGPT.zip
cp -r nft-tracker-v054/. .
rm -rf nft-tracker-v054 NFT-Tracker-v0.5.4-ChatGPT.zip
python -m unittest discover -s tests -t .
git add -A && git commit -m "NFT Tracker v0.5.4" && git push
```
Тесты должны закончиться словом `OK`. Потом на github.com/codespaces удалите этот codespace (чтобы не тратить квоту).

## 3. Секреты
Репозиторий → Settings → Secrets and variables → Actions → **New repository secret**:
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather;
- `PORTALS_API_KEY` — ключ из Portals → Профиль → Partner API;
- `OWNER_CHAT_ID` — пока введите `0`.

## 4. Первый запуск и ваш Telegram ID
1. Вкладка **Actions** → если просит, нажмите «I understand my workflows, enable them».
2. Напишите боту @nfttrecker_bot команду `/id`.
3. Actions → **monitor** → Run workflow. Через ~30 секунд бот ответит вашим числом.
4. Settings → Secrets → `OWNER_CHAT_ID` → Update → вставьте число.

## 5. Проверка подключения к Portals
Actions → **diagnose** → Run workflow → откройте запуск → шаг Diagnose.
Должны быть строки с ✅ для Telegram и Portals. Вывод не содержит секретов; пришлите его мне (или скопируйте) — по нему я настрою формат ссылки на лот. Если видите «Cloudflare блокирует» или «ключ отклонён» — напишите в поддержку Portals (@Portals_Help_Bot), обходить блокировку нельзя и не нужно.

## 6. Включение уведомлений
1. Напишите боту `/start`, затем Actions → monitor → Run workflow (иначе ответ придёт при ближайшем плановом запуске).
2. Дальше всё идёт само, по расписанию примерно каждые 10 минут с возможными задержками GitHub. `/status` покажет состояние.

## Если что-то не так
- Бот молчит: ответы приходят только при запусках, подождите до 15–30 минут или запустите monitor вручную.
- Нет сигналов: `/status` → «Прямая ссылка: не настроена» означает, что сигналы пока блокируются (см. README → «Прямая ссылка»).
- Через 60 дней без изменений в репозитории GitHub отключает расписание: Actions → monitor → Enable workflow.
- Остановить: `/stop` в боте или отключить workflow в Actions. Удалить свои данные: `/delete`.

## Если объявлений оказалось слишком много

Если `/status` показывает «Неполная проверка», можно увеличить `LISTING_PAGES` в GitHub → Settings → Secrets and variables → Actions → Variables (например, с 12 до 20 или до 36). Больше страниц = больше запросов и времени работы. Это не гарантирует, что весь поток объявлений будет обработан при больших объёмах рынка или задержках расписания.

# max-tests-mini-app

Репозиторий подготовлен для сдачи, согласно требованиям содержит:
- Docker-образ проекта
- requirements.txt из образа
- README с инструкцией по запуску

Командой для разработки backend был использован другой репозиторий, где с docker работали собирая образ у себя на машине: https://github.com/Ivan0n/max-test-mini-app-backend
Репозиторий для фронтенда: https://github.com/Ivan0n/max-test-mini-app-front

Само мини-приложение **запущено, работает, подключено к MAX**, хостинг должен продержаться всё время проверки этапа.

## Инструкция по самостоятельному запуску

### 1. Установи и запусти Docker

Установи Docker для своей системы:

- [Windows](https://docs.docker.com/desktop/setup/install/windows-install/).
- [macOS](https://docs.docker.com/desktop/setup/install/mac-install/).
- [Linux: Docker Engine](https://docs.docker.com/engine/install/) и плагин [Docker Compose](https://docs.docker.com/compose/install/linux/).

На Windows и macOS открой **Docker Desktop** и дождись запуска движка.
Приложение должно работать, пока ты пользуешься бэкендом. Если установщик
попросит включить WSL 2 или перезагрузить Windows, выполни это.
На Linux должен работать Docker Engine.

Открой терминал и проверь:

```sh
docker --version
docker compose version
```

Обе команды должны вывести номер версии. Нужен Docker Compose v2 или новее.

### 2. Скачай compose

```sh
git clone https://github.com/Ivan0n/max-tests-mini-app
cd max-tests-mini-app
```

Если проект уже есть, открой **его папку** в VS Code (**Terminal → New Terminal**)
и обнови его: `git pull`. Все команды ниже выполняй в папке бэкенда.

### 3. Запусти compose

```sh
docker compose up -d --wait
```

Первый запуск занимает пару минут: Docker собирает образ. Когда команда
завершится, терминал можно закрыть; Docker Desktop оставь запущенным.

### 4. env

В корневой директории продублируй файл .env.example и убери из него .example, чтобы осталось .env

**В нём находится несколько переменных:**
> MAX_BOT_TOKEN= токен

В MAX_BOT_TOKEN должен быть подставлен токен от бота. (business.max.ru -> Чат-боты -> бот -> Интеграция)
> BOT_ENABLED= (true/false)

BOT_ENABLED отвечает за текстового бота. Пока бот включен на сервере *(он должен быть сейчас включен)*, оставьте **false**, так как два включенных бота с одним токеном будут перехватывать сообщения друг у друга.
> DEV_MODE= (true/false)

Переменная DEV_MODE включает режим разработчика, отключает проверку токена и init data.
> WEB_DEV_LOGIN= (true/false)

WEB_DEV_LOGIN Пропускает авторизацию init data.

> WEB_PORT=127.0.0.1:8080

Адрес сайта на этом компьютере.

<ins>**DEV_MODE и WEB_DEV_LOGIN будут закомментированы, не забудьте разкомментировать их.**</ins>

Остальное (база данных, хранилище картинок, ключи) имеет значения по умолчанию, поэтому не надо их как-либо изменять.

### 5. Проверь

Открой **http://127.0.0.1:8000/api/status**. Должен появиться JSON
`{"message": "Flask server is running", "status": "ok"}`.
По адресу `http://127.0.0.1:8000/` страницы нет: `404` там ожидаемый.

Открой **http://127.0.0.1:5173/**: вход выполнится автоматически под «Разработчиком».
Фронт в `npm run dev` сам обращается к `http://127.0.0.1:8000` и подставляет тестовый
`init_data`, настраивать ничего не нужно.
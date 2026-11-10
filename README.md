# ImpactMap 0.0.3

Перед изменением файла показывает, какие файлы Python и Rust от него зависят,
включая косвенные связи. Можно загрузить публичный репозиторий GitHub или открыть
локальный Git-репозиторий, выбрать файл и увидеть карту потенциального влияния.
Результат можно сохранить как HTML и открыть в любом браузере без установки ImpactMap.

Это рабочий прототип: статические зависимости помогают выбрать места для проверки,
но не доказывают, что изменение сломает программу. Другие языки пока не анализируются.

## Установка и запуск

В [релизе v0.0.3](https://github.com/DarolCup/ImpactMap/releases/tag/v0.0.3)
доступны только **Source code (zip)** и **Source code (tar.gz)**, без готового
установщика. Анализатор нужно один раз собрать. Для простого обновления удобнее
скачать проект через Git по инструкции ниже.

### Fedora Linux с KDE Plasma

Откройте **Konsole** и выполните по порядку:

```sh
sudo dnf install git python3 python3-pyside6 cargo gcc
git clone https://github.com/DarolCup/ImpactMap.git
cd ImpactMap
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
python3 desktop-py/main.py
```

Повторный запуск: откройте терминал в папке `ImpactMap` и выполните
`python3 desktop-py/main.py`. Пересобирать программу при каждом запуске не нужно.

### Windows 10/11

Один раз установите:

1. [Git for Windows](https://git-scm.com/downloads/win).
2. [Python 3.14, Windows installer (64-bit)](https://www.python.org/downloads/windows/).
   При установке оставьте включённым Python Launcher (`py`).
3. [Rust через rustup](https://rust-lang.org/tools/install/), стандартный вариант MSVC.
   Если установщик попросит Visual Studio Build Tools, установите компонент
   **«Разработка классических приложений на C++»** с Windows SDK.

После установки откройте **новое окно PowerShell** и выполните по порядку:

```powershell
git clone https://github.com/DarolCup/ImpactMap.git
cd ImpactMap
py -3.14 -m venv desktop-py\.venv
.\desktop-py\.venv\Scripts\python.exe -m pip install -r desktop-py\requirements.txt
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
.\desktop-py\.venv\Scripts\python.exe desktop-py\main.py
```

Повторный запуск из папки `ImpactMap`:

```powershell
.\desktop-py\.venv\Scripts\python.exe desktop-py\main.py
```

Активировать виртуальное окружение и менять политику выполнения PowerShell не нужно.

### Если скачали ZIP или tar.gz из релиза

Распакуйте архив в отдельную папку. Установите зависимости для своей ОС, как выше,
затем откройте терминал в распакованной папке, где находятся `README.md`,
`analyzer-rs` и `desktop-py`. Пропустите команды `git clone` и `cd ImpactMap`,
выполните остальные команды сборки и запуска. Установленный Git всё равно нужен
для загрузки и анализа репозиториев.

## Как обновлять

Закройте ImpactMap и откройте терминал в папке установленного проекта.
Команды ниже получают актуальную версию из `main`, включая изменения после релиза.

**Fedora KDE:**

```sh
git switch main
git pull --ff-only
sudo dnf upgrade python3-pyside6 cargo
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
python3 desktop-py/main.py
```

**Windows (PowerShell):**

```powershell
git switch main
git pull --ff-only
rustup update stable
.\desktop-py\.venv\Scripts\python.exe -m pip install --upgrade -r desktop-py\requirements.txt
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
.\desktop-py\.venv\Scripts\python.exe desktop-py\main.py
```

Если Git сообщает о локальных правках или расхождении веток, сначала сохраните
свои изменения; эти команды не сбрасывают их принудительно.

**Установка из архива:** `git pull` для неё не работает. Скачайте исходники нового
релиза, распакуйте в новую папку и повторите сборку и установку Python-зависимостей.
Если хотите обновляться короткими командами выше, вместо архива используйте `git clone`.

## Первая проверка

1. Выберите GitHub и вставьте HTTPS-ссылку либо выберите локальную папку.
2. Нажмите «Проверить изменения». Для первого знакомства есть «Попробовать на примере».
3. В списке **«До изменения файла»** выберите исходник. Прогноз работает даже без
   фактических изменений. Карта показывает прямые и косвенные зависимости.
4. Пункт «Показать проверенные изменения» возвращает результат сравнения.
5. «Сохранить HTML…» сохраняет открытый результат для передачи другим людям.

Стрелка `A → B` означает: A использует B. При изменении B проверять нужно A.
Для обновления снимка снова нажмите «Проверить изменения».
Если анализатор не собран, только учебный пример может показывать явно помеченный
сохранённый результат; предварительная оценка в нём недоступна.

## Быстрая проверка без графического интерфейса

После установки Git, Python и Rust выполните из корня проекта (на Windows замените
`python3` на `py -3.14`):

```sh
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
python3 desktop-py/report_cli.py --demo --file helpers.py --output impactmap-report.html
```

Откройте `impactmap-report.html` в браузере. Ожидается два зависимых файла:
`app.py → helpers.py` и `runner.py → app.py → helpers.py`.
PySide6 для создания HTML не нужен. Анализ выполняет настоящий Rust-бинарник.

Свой репозиторий:

```sh
python3 desktop-py/report_cli.py --github владелец/репозиторий --file src/helpers.py --output impactmap-report.html
python3 desktop-py/report_cli.py --local /путь/к/репозиторию --file helpers.py --output impactmap-report.html
```

Замените путь после `--file` на существующий путь относительно корня репозитория.
Без `--file` получится отчёт о фактических изменениях. Нужен хотя бы один Git-коммит.
Для приватного проекта используйте заранее клонированную локальную папку.

## Как устроено и что пока ограничено

- Rust строит граф импортов и сравнивает снимки; Python управляет Git, интерфейсом
  и отчётами. Это две части одного приложения.
- GitHub: публичный репозиторий, ветка по умолчанию, последний коммит и его первый
  родитель. Выбора PR/ветки и авторизации приватных репозиториев пока нет.
- Локально: HEAD и рабочая папка, включая новые неигнорируемые файлы.
- Прогноз использует текущий снимок и не переносит в него исторические связи или
  находки о старых изменениях. Граф пересчитывается локально без сетевого запроса.
- Поддерживаются импорты Python и `mod`/`use` Rust. Динамические импорты, макросы,
  обычные обращения Rust без `use`, FFI и другие языки покрыты не полностью либо
  не покрыты. Предупреждения сохраняются в интерфейсе и отчёте.
- Для нестандартных папок Python добавьте в корень проекта `.impactmap.json`:
  `{"python_source_roots": ["desktop-py", "services/backend"]}`.
- Код анализируемого проекта не запускается. Сеть нужна для загрузки с GitHub
  и первоначальной установки зависимостей.

[Контракт анализатора](analyzer-rs/README.md) · [Клиент](desktop-py/README.md) ·
[Разбор проекта и передача следующему агенту](HANDOFF.md)

# ImpactMap

Тестовая версия. Показывает, какие файлы Rust и Python затрагивают локальные изменения Git. Команды запускайте из корня распакованных исходников.

## Fedora Linux KDE

```sh
sudo dnf install git python3-pyside6 cargo gcc
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
python3 desktop-py/main.py
```

## Windows (PowerShell)

Установите Git, Python 3.14 и Rust (MSVC с Visual Studio Build Tools и пакетом «Разработка классических приложений на C++»).

```powershell
cargo build --release --locked --manifest-path analyzer-rs/Cargo.toml
py -3.14 -m venv desktop-py\.venv
.\desktop-py\.venv\Scripts\python.exe -m pip install -r desktop-py\requirements.txt
.\desktop-py\.venv\Scripts\python.exe desktop-py\main.py
```

Выберите локальный Git-репозиторий с коммитом и нажмите «Проверить изменения». Стрелка `A → B` означает, что `A` использует `B`. Без собранного анализатора приложение показывает пример. Сеть нужна только для установки зависимостей.

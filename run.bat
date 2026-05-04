@echo off
echo "Configurando el entorno y ejecutando la aplicacion..."

REM Comprobar si Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo "Python no esta instalado o no esta en el PATH."
    pause
    exit /b 1
)

REM Crear el entorno virtual si no existe
if not exist venv (
    echo "Creando entorno virtual..."
    python -m venv venv
)

REM Instalar las dependencias usando el pip del entorno virtual
echo "Instalando dependencias..."
venv\Scripts\pip.exe install -r requirements.txt

REM Ejecutar la aplicacion en la IP 0.0.0.0 y el puerto 8020
echo.
echo "------------------------------------------------------------------"
echo "  Iniciando la aplicacion..."
echo "  - Para acceder localmente, usa: http://127.0.0.1:8020"
echo "  - Para acceder desde otros dispositivos en la red, usa: http://192.168.1.15:8020"
echo "  (Asegurate de que 192.168.1.15 es la IP correcta de esta maquina)"
echo "  (Recuerda permitir el puerto 8020 en el Firewall de Windows)"
echo "------------------------------------------------------------------"
echo.
venv\Scripts\python.exe -m flask run --host=0.0.0.0 --port=8020

pause
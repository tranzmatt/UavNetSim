@echo off
setlocal

set "PROJECT_ROOT=%~dp0..\.."
set "JAVA_EXE="
if exist "%PROJECT_ROOT%\tools\java\bin\java.exe" set "JAVA_EXE=%PROJECT_ROOT%\tools\java\bin\java.exe"
if not defined JAVA_EXE for /d %%D in ("%PROJECT_ROOT%\tools\java\*") do if exist "%%~fD\bin\java.exe" if not defined JAVA_EXE set "JAVA_EXE=%%~fD\bin\java.exe"
if not exist "%JAVA_EXE%" (
  echo Project-local Java was not found under tools\java.
  exit /b 1
)

set "OSM2WORLD_JAR="
for /r "%~dp0" %%F in (*osm2world*.jar) do if not defined OSM2WORLD_JAR set "OSM2WORLD_JAR=%%F"
if not defined OSM2WORLD_JAR for /r "%~dp0" %%F in (*.jar) do if not defined OSM2WORLD_JAR set "OSM2WORLD_JAR=%%F"
if not defined OSM2WORLD_JAR (
  echo OSM2World JAR was not found under tools\osm2world.
  exit /b 1
)

pushd "%~dp0"
"%JAVA_EXE%" --add-exports java.base/java.lang=ALL-UNNAMED --add-exports java.desktop/sun.awt=ALL-UNNAMED --add-exports java.desktop/sun.java2d=ALL-UNNAMED -jar "%OSM2WORLD_JAR%" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

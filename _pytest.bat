@echo off
set "PY="
for %%P in ("py -V:3.14-arm64" "py -3-64" "py -3" "py") do (
    if not defined PY (
        %%~P -c "pass" >/dev/null 2>&1 && set "PY=%%~P"
    )
)
echo Selected: %PY%
%PY% -c "import sys; print(sys.version)"

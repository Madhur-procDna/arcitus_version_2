Arcetus sample data (SQLite mode)
================================

Place your workbook here so the API can load it without editing code:

  Backend\data\Arcutis Dummy Data v1.xlsx

Alternatively, set in Backend\src\.env:

  DATA_FILE_PATH=C:\full\path\to\your_file.xlsx

Default config expects ``data/Arcutis Dummy Data v1.xlsx`` under the Backend folder. If that
file is missing, the app also looks under ``Desktop\sql\`` (including OneDrive Desktop
variants) for common Arcetus sample names.

Then start the stack:

  1. Backend:  e.g. ``cd Backend\src`` then ``uvicorn api_server:app --reload --host 127.0.0.1 --port 8000``
     (or ``.\Backend\run_api.ps1`` for port **8001**)
  2. Frontend: ``cd Frontendd`` then ``npm run dev`` — set ``SDA_BACKEND_URL`` in ``Frontendd\.env.local`` to the **same** host:port as uvicorn.

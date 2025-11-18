# app.py
# simple shim: import your main Flask app
try:
    from server import app  # preferred name
except Exception:
    # fallback: try 'main' module
    from importlib import import_module
    mod = import_module("main")
    app = getattr(mod, "app")

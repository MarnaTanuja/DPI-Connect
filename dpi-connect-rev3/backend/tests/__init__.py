import os

# Configure the environment once, before any test module imports the app.
os.environ["DPI_CONNECT_MONGO_MOCK"] = "1"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ.pop("MISTRAL_API_KEY", None)   # deterministic: lexicon scoring + fuzzy fallback only

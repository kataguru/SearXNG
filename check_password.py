import os

# Load .env same way as migration script
def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

load_env()

password = os.environ.get("PAPERLESS_ADMIN_PASSWORD", "NOT SET")
print(f"Password from .env: {repr(password)}")
print(f"Length: {len(password)}")

import requests

# Test with single $ (Docker Compose interpretation of $$)
password_single = "Paperl3ss!Ngx#2024$Secure"
r1 = requests.get('http://localhost:8010/api/documents/', auth=('admin', password_single), timeout=10)
print(f"Single $ - Status: {r1.status_code}")
if r1.status_code == 200:
    print("SUCCESS with single $")
else:
    print(r1.text[:200])

# Test with double $$ (literal from .env)
password_double = "Paperl3ss!Ngx#2024$$Secure"
r2 = requests.get('http://localhost:8010/api/documents/', auth=('admin', password_double), timeout=10)
print(f"\nDouble $$ - Status: {r2.status_code}")
if r2.status_code == 200:
    print("SUCCESS with double $$")
else:
    print(r2.text[:200])

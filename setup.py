import subprocess
import sys

print("Installing requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)

print("Installing Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

print("\n✅ Setup complete!")
print("   1. Copy config/app_config.example.json to config/app_config.json and fill in your Supabase details.")
print("   2. Run 'python main.py' to start FLINT.")
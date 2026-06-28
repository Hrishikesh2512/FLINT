import subprocess
import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"FLINT needs Python 3.11+. You are on {sys.version.split()[0]}. "
        "Please upgrade and re-run setup."
    )

print("Installing requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)

print("Installing Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

print("\n✅ Setup complete! Run 'python main.py' to start FLINT.")
"""
build.py
Automates the PyInstaller build process and packages the resulting
executable into a clean .zip file for GitHub Releases.
"""
import os
import shutil
import subprocess
import sys
import re
from pathlib import Path

# --- Extract Version dynamically from constants.py ---
def get_version():
    try:
        with open("constants.py", "r", encoding="utf-8") as f:
            # Looks for a line like: VERSION = "1.1" or APP_VERSION = '1.1'
            match = re.search(r'VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', f.read(), re.IGNORECASE)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"⚠️ Could not parse version from constants.py: {e}")
    return "Unknown" # Fallback if not found

APP_VERSION = get_version()

# --- Configuration ---
APP_NAME = "KiCad Library Manager"
MAIN_SCRIPT = "main.py"
RELEASE_ZIP_NAME = f"KiCad_Library_Manager_v{APP_VERSION}"

# Extra files to bundle in the ZIP alongside the executable
EXTRA_FILES = ["README.md", "LICENSE"]

def clean_old_builds():
    print("🧹 Cleaning old build directories...")
    for path in ["build", "dist"]:
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"   Removed {path}/")
            
    for f in os.listdir("."):
        if f.endswith(".spec"):
            os.remove(f)
            print(f"   Removed {f}")

def run_pyinstaller():
    print(f"🚀 Running PyInstaller for '{APP_NAME}' (v{APP_VERSION})...")
    
    # Base command for a single-file, windowed (no terminal) app
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        f"--name={APP_NAME}",
    ]
    
    # OS-independent separator for add-data (; on Windows, : on Mac/Linux)
    sep = os.pathsep
    
    # Attach icon to the .exe metadata AND bundle it inside the .exe for the UI to use
    if os.path.exists("icon.ico"):
        cmd.append("--icon=icon.ico")
        cmd.append(f"--add-data=icon.ico{sep}.")
    elif os.path.exists("icon.png"):
        if os.name == 'nt':
            print("⚠️ Note: Windows strictly prefers .ico files for executables.")
            print("   If your icon doesn't show up, convert your icon.png to an icon.ico file!")
        cmd.append("--icon=icon.png")
        cmd.append(f"--add-data=icon.png{sep}.")
        
    # Target script
    cmd.append(MAIN_SCRIPT)
    
    try:
        subprocess.run(cmd, check=True)
        print("✅ PyInstaller build complete.")
    except subprocess.CalledProcessError as e:
        print(f"❌ PyInstaller failed with error: {e}")
        sys.exit(1)

def create_release_zip():
    print(f"📦 Packaging release into {RELEASE_ZIP_NAME}.zip...")
    
    # Create a temporary staging folder
    staging_dir = Path("staging_release")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir()
    
    # Determine OS-specific executable name
    exe_name = f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME
    exe_path = Path("dist") / exe_name
    
    if exe_path.exists():
        shutil.copy2(exe_path, staging_dir / exe_name)
        print(f"   Added {exe_name}")
    else:
        print(f"❌ Error: Could not find compiled executable at {exe_path}")
        shutil.rmtree(staging_dir)
        sys.exit(1)

    # Include extra files (README, License, etc.) if they exist in the root
    for extra_file in EXTRA_FILES:
        if Path(extra_file).exists():
            shutil.copy2(extra_file, staging_dir / extra_file)
            print(f"   Added {extra_file}")

    # Create the zip archive from the staging folder
    shutil.make_archive(RELEASE_ZIP_NAME, 'zip', staging_dir)
    
    # Clean up the staging folder
    shutil.rmtree(staging_dir)
    print(f"🎉 Success! '{RELEASE_ZIP_NAME}.zip' is ready for your GitHub Release.")

if __name__ == "__main__":
    print("========================================")
    print("  KiCad Library Manager Build Pipeline  ")
    print("========================================\n")
    
    # Make sure PyInstaller is actually installed
    try:
        import PyInstaller
    except ImportError:
        print("❌ PyInstaller is not installed. Please run: pip install pyinstaller")
        sys.exit(1)
        
    clean_old_builds()
    print("")
    run_pyinstaller()
    print("")
    create_release_zip()
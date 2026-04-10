# KiCad Custom Library Manager

A standalone GUI application for organizing, managing, and health-checking custom KiCad (v6/v7/v8/v9+) component libraries.

This tool treats a component as a unified "Asset Bundle" (Symbol, Footprint, 3D Model, Datasheet, Image), ensuring physical files stay synchronized with their KiCad symbol properties.

## Features

* 📦 Drag-and-Drop Import: Drop .zip, .elibz (EasyEDA), or loose .kicad_sym/.kicad_mod files directly into the UI to parse and stage them. Supports alternate footprint variations via prefix naming.

* 🪄 Digi-Key API Integration: Automatically fetch metadata, part numbers, datasheets, and component photos.

* 🏥 Library Health Scanner: Scans your physical hard drive and .kicad_sym files to detect duplicates, orphaned files, broken links, and misplaced assets.

* 🔗 KiCad Auto-Sync: Automatically injects your custom library directories and custom environment variables into KiCad's global sym-lib-table and fp-lib-table.

## Getting Started

### Option 1: Standalone Executable (Recommended)

The easiest way to use the manager is via the pre-compiled standalone executable.

1. Download the latest release from the Releases page.

2. Run the executable directly—no Python installation or setup required.



### Option 2: Running from Source (Python Scripts)

If you wish to run the raw Python scripts or contribute to the development, follow these setup steps:

1. Clone the repository:
```bash
git clone [https://github.com/MakingACompany/KiCad-Library-Manager.git](https://github.com/MakingACompany/KiCad-Library-Manager.git)
cd KiCad-Library-Manager
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the application:
```bash
python main.py
```

## Initial Application Setup

1. Open the application and click the Settings (⚙️) icon.

2. Select your Library Root Folder (where you want all your KiCad symbols and footprints saved).

3. Set your custom KiCad Path Variable (e.g., ${CUSTOM_LIB_DIR}) to keep 3D model paths portable across different machines.

## Digi-Key API Setup (Optional but Highly Recommended)

To enable the "Smart Fetch" feature that automatically downloads metadata, datasheets, and images, you will need to provide a free Digi-Key Developer API key.

### Step-by-Step Guide:

1. Go to https://developer.digikey.com/ and click Register to create a free developer account.

2. Once logged in, navigate to Organizations and click Create an Organization. Provide a name for your organization.

3. Go to Production Apps and click Create Production App.

4. Enter an App name (e.g., "KiCad Library Manager").

5. OAuth2 Setup: * Note: This is the external site to which a consumer of this app is redirected to log in when using three-legged OAuth.

* For both Windows and Linux desktop environments, you can simply enter a generic local callback address here, such as https://localhost or http://localhost:8080.

6. Scroll down to the APIs section and add the following abilities:

* Product Information V4

* Reference APIs

7. Click Create or Save.

8. You will now be provided with a Client ID and a Client Secret.

9. Back in the KiCad Library Manager, go to Settings -> Integrations. Paste your Client ID and Client Secret into the respective fields and click Test API Credentials. Save your settings.

## Standard Workflow

1. Drag downloaded component archives or loose files into the main drop zone.

2. Verify or auto-fill the part data in the Symbol Editor (click the Magic Wand icon to fetch from Digi-Key).

3. Click Import Component.

4. To link the newly updated library to KiCad natively, go to File -> Add Libraries to KiCad... and point it to your specific KiCad configuration folder (e.g., AppData/Roaming/kicad/8.0).
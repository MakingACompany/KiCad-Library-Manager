"""
constants.py
Centralized configuration for categories and standardized component metadata fields.
"""

APP_VERSION = "1.1"

# Default Categories with descriptions (Users can edit/add these in the Settings UI)
DEFAULT_CATEGORIES = {
    "Audio": "Audio components like speakers, microphones, and buzzers.",
    "Assemblies": "Pre-assembled boards and modules.",
    "Buttons_Switches": "Tactile buttons, toggles, and slide switches.",
    "Connectors": "Headers, jacks, and terminal blocks.",
    "Displays": "OLEDs, LCDs, and segment displays.",
    "ICs": "Integrated Circuits and complex silicon.",
    "LEDs": "Light emitting diodes and indicators.",
    "Logos_Graphics": "Silkscreen graphics and PCB art.",
    "Mechanical": "Heatsinks, standoffs, and hardware.",
    "Microcontrollers": "MCUs, MPUs, and development chips.",
    "Modules": "Drop-in subcircuits and breakouts.",
    "Passive": "Resistors, capacitors, and inductors.",
    "Power": "Regulators, LDOs, and power management.", 
    "Sensors": "Environmental and motion sensors.",
    "Transistors": "MOSFETs, BJTs, and discrete semiconductors."
}

# Default Subcategories mapped to their parent Category
DEFAULT_SUBCATEGORIES = {
    "Audio": ["Amplifiers", "Buzzers", "Microphones", "Speakers"],
    "Assemblies": ["Development Boards", "Power Modules", "Sensor Modules"],
    "Buttons_Switches": ["Tactile", "Toggle", "Slide", "Pushbutton"],
    "Connectors": ["Headers", "Jacks", "Terminal Blocks", "USB", "RF"],
    "Displays": ["OLED", "LCD", "TFT", "7-Segment", "E-Paper"],
    "ICs": ["Logic", "Memory", "Timers", "Drivers"],
    "LEDs": ["SMD", "THT", "RGB", "High Power", "Infrared"],
    "Logos_Graphics": ["Warning", "Brand", "Open Source", "ESD"],
    "Mechanical": ["Heatsinks", "Standoffs", "Screws", "Enclosures"],
    "Microcontrollers": ["ARM", "AVR", "RISC-V", "PIC", "ESP32"],
    "Modules": ["Wireless", "GPS", "Bluetooth", "WiFi", "RFID"],
    "Passive": ["Resistors", "Capacitors", "Inductors", "Ferrite Beads", "Transformers", "Fuses"],
    "Power": ["LDOs", "Switching Regulators", "PMICs", "Battery Management", "Converters"],
    "Sensors": ["Temperature", "Pressure", "Motion", "Optical", "Magnetic", "Gas"],
    "Transistors": ["MOSFETs", "BJTs", "IGBTs", "JFETs", "Arrays"]
}

PART_FIELDS = {
    "Basic Data": [
        {"key": "Description", "label": "Description", "type": "text"},
        {"key": "MPN", "label": "Manufacturer Part Number", "type": "text"},
        {"key": "Manufacturer", "label": "Manufacturer", "type": "text"},
        {"key": "Subcategory", "label": "Sub Category", "type": "subcategory_list"},
        {"key": "Package", "label": "Package / Case", "type": "text"},
        {"key": "Mounting Type", "label": "Mounting Type (SMD/TH)", "type": "text"},
        {"key": "ki_keywords", "label": "KiCad Keywords (Search Tags)", "type": "text"},
    ],
    "Advanced": [
        # Native KiCad Core Links
        {"key": "Datasheet", "label": "Datasheet", "type": "file_datasheet"},
        {"key": "Footprint", "label": "Footprint", "type": "file_footprint"},

        # Extended Component Specifications
        {"key": "Tolerance", "label": "Tolerance", "type": "text"},
        {"key": "Power Rating", "label": "Power Rating", "type": "text"},
        {"key": "Voltage Rating", "label": "Voltage Rating", "type": "text"},
        {"key": "Current Rating", "label": "Current Rating", "type": "text"},
        {"key": "Operating Temperature", "label": "Operating Temp", "type": "text"},

        # Sourcing / Vendor Properties
        {"key": "Supplier", "label": "Supplier (e.g. Digikey, LCSC, Amazon)", "type": "text"},
        {"key": "Supplier Part", "label": "Supplier Part Number", "type": "text"},
        {"key": "DigiKey_PN", "label": "DigiKey Part Number", "type": "text"},
        {"key": "LCSC_PN", "label": "LCSC Part Number (Cxxxx)", "type": "text"},
        {"key": "Mouser_PN", "label": "Mouser Part Number", "type": "text"}
    ]
}

MANUAL_HTML = f"""
<h2>KiCad Custom Library Manager v{APP_VERSION} - Quick Guide</h2>

<h3>1. Importing Parts</h3>
<p><b>Drag and Drop:</b> Drop <code>.zip</code> files (from SnapEDA, UltraLibrarian, ComponentSearchEngine), <code>.elibz</code> files (from EasyEDA), or loose <code>.kicad_sym</code> / <code>.kicad_mod</code> files directly onto the Main Drop Zone.</p>
<p>The manager automatically parses the files, extracts 3D models and datasheets, and opens the Symbol Editor for review.</p>

<h3>2. Alternate Footprints & Logos</h3>
<p>You can maintain multiple footprints for a single part (e.g., standard vs hand-soldering). If your symbol is named <code>eFuse</code>, any footprint dragged into the app named <code>eFuse_HandSolder.kicad_mod</code> or <code>eFuse-Alternate.kicad_mod</code> will be automatically assigned to that component without triggering orphaned file warnings.</p>

<h3>3. Digi-Key API Auto-Fill</h3>
<p>If configured in Settings, the app automatically queries Digi-Key during imports using the component's MPN or Name. It downloads Datasheets, component photos, and populates missing metadata fields like Description, Manufacturer, and exact part numbers.</p>
<h4>How to Setup the Digi-Key API:</h4>
<ol>
    <li>Go to <a href="https://developer.digikey.com/">developer.digikey.com</a> and click <b>Register</b> to create a free developer account.</li>
    <li>Once logged in, navigate to <b>Organizations</b> and click <b>Create an Organization</b>. Provide a name for your organization.</li>
    <li>Go to <b>Production Apps</b> and click <b>Create Production App</b>.</li>
    <li>Enter an <b>App name</b> (e.g., "KiCad Library Manager").</li>
    <li><b>OAuth2 Setup:</b> <i>(This is the external site to which a consumer of this app is redirected to log in when using three-legged OAuth.)</i> For both Windows and Linux desktop environments, you can simply enter a generic local callback address here, such as <code>https://localhost</code> or <code>http://localhost:8080</code>.</li>
    <li>Scroll down to the <b>APIs</b> section and <b>add the following abilities</b>:
        <ul>
            <li><b>Product Information V4</b></li>
            <li><b>Reference APIs</b></li>
        </ul>
    </li>
    <li>Click <b>Create</b> or <b>Save</b>. You will now be provided with a <b>Client ID</b> and a <b>Client Secret</b>.</li>
    <li>Back in the <b>KiCad Library Manager</b>, go to <b>Settings -> Integrations</b>. Paste your Client ID and Client Secret into the respective fields and click <b>Test API Credentials</b>. Save your settings.</li>
</ol>

<h3>4. Health Scanner</h3>
<p>Switch to the <b>Health Scanner</b> tab to check your library for issues:</p>
<ul>
    <li><b>Duplicates:</b> Parts with the same name, MPN, or Supplier Part Number. Select them and click "Merge Selected" to cleanly combine their data.</li>
    <li><b>Orphaned Files:</b> Physical footprints or 3D models sitting on your hard drive that aren't linked to any symbol. Delete them or securely link them back.</li>
    <li><b>Misplaced Files:</b> Assets saved in the wrong folder category. Automatically move them to the correct location and update internal symbol links.</li>
    <li><b>Broken Links:</b> Symbols pointing to a footprint or datasheet that no longer exists.</li>
</ul>

<h3>5. Using in KiCad</h3>
<p>Go to <b>File -> Add Libraries to KiCad...</b>. This will automatically inject your Custom Library folders into KiCad's global <code>sym-lib-table</code> and <code>fp-lib-table</code> files.</p>
<p><b>Portable 3D Models:</b> Be sure to define a custom <b>KiCad Path Variable</b> (like <code>${{CUSTOM_LIB_DIR}}</code>) in Settings. This keeps your 3D models working perfectly even if you move your library folder to another PC!</p>
"""

ABOUT_HTML = f"""
<h2>KiCad Custom Library Manager</h2>
<p><b>Version:</b> {APP_VERSION}</p>
<p><b>Author:</b> Aaron Loar<br>
<b>Company:</b> MakingA Company<br>
<b>Year:</b> 2026</p>
<p><i>Developed to streamline KiCad component management and improve PCB design workflows.</i></p>
"""
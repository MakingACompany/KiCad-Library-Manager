"""
digikey_api.py
Handles OAuth2 token generation and Product API querying for Digi-Key.
"""
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import logging
from PySide6.QtCore import QSettings
from pathlib import Path

logger = logging.getLogger(__name__)

class DigiKeyAPI:
    def __init__(self):
        self.settings = QSettings("OpenSourceTools", "KiCadLibManager")
        
        # Helper to prevent 'None' from turning into the literal string "None"
        def safe_str(key):
            val = self.settings.value(key, "")
            if val is None: return ""
            val_str = str(val).strip()
            return "" if val_str == "None" else val_str
            
        # Safely extract strings
        self.client_id = safe_str("dk_client_id")
        self.client_secret = safe_str("dk_client_secret")
        self.token = safe_str("dk_token")
        
        # Safely parse the float for token expiration
        expiry_val = self.settings.value("dk_token_expiry", 0.0)
        try:
            # Wrapping in str() satisfies strict type-checkers like Pylance
            self.token_expiry = float(str(expiry_val)) if expiry_val else 0.0
        except (ValueError, TypeError):
            self.token_expiry = 0.0

    def is_configured(self):
        return bool(self.client_id and self.client_secret)

    def test_credentials(self, client_id, client_secret):
        """Tests the provided credentials directly and returns detailed error messages."""
        if not client_id or not client_secret:
            return False, "Client ID or Client Secret is missing."

        url = "https://api.digikey.com/v1/oauth2/token"
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials"
        }).encode('utf-8')

        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10) as response:
                res = json.loads(response.read().decode('utf-8'))
                if res.get("access_token"):
                    return True, "Successfully authenticated with Digi-Key!"
                return False, "Connected, but no access token was returned."
        except urllib.error.HTTPError as e:
            try:
                err_text = e.read().decode('utf-8')
                err_json = json.loads(err_text)
                # Digi-Key OAuth errors usually have 'errorMessage' or 'error_description'
                msg = err_json.get("errorMessage", err_json.get("error_description", err_text))
            except Exception:
                msg = str(e)
            return False, f"HTTP Error {e.code}: {msg}"
        except Exception as e:
            return False, f"Connection Failed: {str(e)}"

    def _get_token(self):
        if not self.is_configured():
            return None
            
        # Return cached token if it's still valid for at least another minute
        if self.token and time.time() < self.token_expiry:
            return self.token

        url = "https://api.digikey.com/v1/oauth2/token"
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }).encode('utf-8')

        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10) as response:
                res = json.loads(response.read().decode('utf-8'))
                self.token = res.get("access_token")
                # Expire 60 seconds early for safety
                self.token_expiry = time.time() + float(res.get("expires_in", 3600)) - 60 
                
                self.settings.setValue("dk_token", self.token)
                self.settings.setValue("dk_token_expiry", self.token_expiry)
                return self.token
        except urllib.error.HTTPError as e:
            logger.error(f"Digi-Key Auth Failed (HTTP {e.code}): {e.read().decode('utf-8')}")
            return None
        except Exception as e:
            logger.error(f"Digi-Key Token Error: {e}")
            return None

    def _map_category(self, dk_category, dk_family):
        """Attempts to map a Digi-Key category/family to a standard KLC category."""
        text = f"{dk_category} {dk_family}".lower()
        
        mapping = {
            "resistor": "Resistors",
            "capacitor": "Capacitors",
            "inductor": "Inductors",
            "diode": "Diodes",
            "transistor": "Transistors",
            "fet": "Transistors",
            "mosfet": "Transistors",
            "connector": "Connectors",
            "terminal block": "Connectors",
            "contact": "Connectors",
            "integrated circuit": "Integrated_Circuits",
            "ic": "Integrated_Circuits",
            "logic": "Integrated_Circuits",
            "microcontroller": "Integrated_Circuits",
            "mcu": "Integrated_Circuits",
            "memory": "Integrated_Circuits",
            "optoelectronic": "LEDs",
            "led": "LEDs",
            "display": "Displays",
            "switch": "Switches",
            "relay": "Relays",
            "oscillator": "Oscillators",
            "crystal": "Oscillators",
            "sensor": "Sensors",
            "transformer": "Transformers",
            "power supply": "Power_Supplies",
            "converter": "Power_Supplies",
            "rf": "RF_Modules",
            "isolator": "Isolators",
            "optocoupler": "Isolators",
            "hardware": "Mechanical",
            "standoff": "Mechanical",
            "battery": "Battery_Holders"
        }
        
        for key, cat in mapping.items():
            if key in text:
                return cat
        return ""

    def search_part(self, keyword):
        """Searches Digi-Key for an MPN or keyword and maps the results to standard KLC fields."""
        token = self._get_token()
        if not token:
            return None
            
        # Helper to aggressively sanitize API strings (converts " to 'in', removes newlines)
        def clean_text(val):
            if not val: return ""
            return str(val).replace('"', 'in').replace('\n', ' ').replace('\r', '').strip()

        url = "https://api.digikey.com/products/v4/search/keyword"
        headers = {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "Content-Type": "application/json"
        }
        
        # SMART RETRY LOGIC: Maximize fallback potential for weird Wago / Connectors
        keywords_to_try = [keyword]
        if "_" in keyword:
            keywords_to_try.append(keyword.replace("_", "/"))
            keywords_to_try.append(keyword.replace("_", "-"))
        if "/" in keyword:
            # Digi-Key frequently omits slashes from their backend search indexes
            keywords_to_try.append(keyword.replace("/", "-"))
            keywords_to_try.append(keyword.replace("/", ""))

        for kw in keywords_to_try:
            payload = {
                "Keywords": kw,
                "RecordCount": 1
            }

            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
                with urllib.request.urlopen(req, timeout=15) as response:
                    res = json.loads(response.read().decode('utf-8'))
                    
                    # V4 robust check: Check ExactMatches first, then fallback to general Products array
                    p = res.get("ExactDigiKeyProduct")
                    if not p:
                        products = res.get("ExactMatches", [])
                        if not products:
                            products = res.get("Products", [])
                        if not products:
                            logger.info(f"Digi-Key: No results found for '{kw}'")
                            continue # Try the next fallback keyword!
                        p = products[0]

                    # Robust Manufacturer Extraction
                    mfg_obj = p.get("Manufacturer") or {}
                    if isinstance(mfg_obj, dict):
                        mfg_name = mfg_obj.get("Name") or mfg_obj.get("Value") or ""
                    else:
                        mfg_name = str(mfg_obj)
                        
                    # Robust Description Extraction - Prioritizing DetailedDescription
                    desc_obj = p.get("Description") or {}
                    if isinstance(desc_obj, dict):
                        desc_str = desc_obj.get("DetailedDescription") or desc_obj.get("ProductDescription") or ""
                    else:
                        desc_str = str(desc_obj)
                    
                    if not desc_str or desc_str == "{}":
                        desc_str = p.get("DetailedDescription") or p.get("ProductDescription") or ""

                    # V4 Product / Part Number Extractions
                    mpn = p.get("ManufacturerProductNumber") or p.get("ManufacturerPartNumber") or ""
                    
                    # Category mapping for auto-selection
                    cat_obj = p.get("Category", {})
                    fam_obj = p.get("Family", {})
                    dk_cat = cat_obj.get("Name", "") if isinstance(cat_obj, dict) else str(cat_obj)
                    dk_fam = fam_obj.get("Name", "") if isinstance(fam_obj, dict) else str(fam_obj)
                    
                    # In V4, DigiKey numbers are often pushed into the Variations array (e.g. Cut Tape vs Tape & Reel)
                    dk_pn = ""
                    variations = p.get("ProductVariations", [])
                    if variations:
                        # Prefer Cut Tape (CT) if available
                        for var in variations:
                            pkg_name = var.get("PackageType", {}).get("Name", "")
                            if "Cut Tape" in pkg_name or "(CT)" in pkg_name:
                                dk_pn = var.get("DigiKeyProductNumber") or ""
                                break
                        
                        # If no Cut Tape was found, default to the first available variation
                        if not dk_pn:
                            dk_pn = variations[0].get("DigiKeyProductNumber") or ""
                    else:
                        dk_pn = p.get("DigiKeyPartNumber") or p.get("DigiKeyProductNumber") or ""
                        
                    url_out = p.get("ProductUrl") or p.get("Url") or ""
                    ds_url = p.get("DatasheetUrl") or p.get("PrimaryDatasheet") or ""

                    mapped = {
                        "Manufacturer": clean_text(mfg_name),
                        "MPN": clean_text(mpn),
                        "Description": clean_text(desc_str),
                        "URL": url_out,         # URLs are intentionally left un-sanitized to avoid breaking links
                        "Datasheet": ds_url, 
                        "DigiKey_PN": clean_text(dk_pn),
                        "Supplier": "Digi-Key" if dk_pn else "",
                        "Supplier Part": clean_text(dk_pn)
                    }
                    
                    # Attach the "Jump Start" category
                    suggested_cat = self._map_category(dk_cat, dk_fam)
                    if suggested_cat:
                        mapped["Suggested_Category"] = suggested_cat
                    
                    # Parameter Scraping (Targeting V4 ParameterText and ValueText keys)
                    for param in p.get("Parameters", []):
                        pn = param.get("ParameterText") or param.get("Parameter") or ""
                        pv = clean_text(param.get("ValueText") or param.get("Value") or "")
                        
                        if "Tolerance" in pn: mapped["Tolerance"] = pv
                        elif "Power" in pn and "W" in pn: mapped["Power Rating"] = pv
                        elif "Voltage" in pn: mapped["Voltage Rating"] = pv
                        elif "Current" in pn: mapped["Current Rating"] = pv
                        elif "Temperature" in pn: mapped["Operating Temperature"] = pv
                        elif "Package" in pn and "Case" in pn: mapped["Package"] = pv
                        elif "Mounting Type" in pn: mapped["Mounting Type"] = pv

                    # Strip out empty results
                    return {k: v for k, v in mapped.items() if v and str(v).strip() != "-"}
                    
            except urllib.error.HTTPError as e:
                logger.error(f"Digi-Key Search Failed (HTTP {e.code}) for '{kw}': {e.read().decode('utf-8')}")
                continue
            except Exception as e:
                logger.error(f"Digi-Key Search Error for '{kw}': {e}")
                continue

        # Exhausted all fallback queries
        return None

    def download_datasheet(self, url, dest_path):
        """Downloads a generic URL to a local destination."""
        if not url or str(url).strip() == "-": return False
        
        # PROTOCOL-RELATIVE URL FIX: Translates "//mm.digikey.com..." to "https://mm.digikey.com..."
        if url.startswith("//"):
            url = "https:" + url
            
        try:
            # Using a comprehensive User-Agent to bypass strict server anti-bot protections
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/pdf,application/octet-stream,*/*'
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                content_type = response.headers.get('Content-Type', '').lower()
                # Confirm it actually returned a PDF, not an HTML error/login page
                if 'application/pdf' in content_type or 'octet-stream' in content_type or url.lower().split('?')[0].endswith('.pdf'):
                    with open(dest_path, 'wb') as f:
                        f.write(response.read())
                    return True
        except Exception as e:
            logger.error(f"Digi-Key Auto-Datasheet download failed: {e}")
        return False
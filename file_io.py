"""
file_io.py

File Manager:
Handles ZIP extraction, file staging in the .tmp directory, and file moving operations.
Contains a modular format detection system for identifying assets via file-sniffing
and auto-converting Third-Party formats to KiCad formats.

Kicad I/O:
Bulletproof read/write engine for KiCad V6+ library files.
Converts physical .kicad_sym files into in-memory Symbol and Category data models,
and precisely serializes them back to disk. Actively repairs formatting and corruption.
"""

import datetime
import json
import logging
import os
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Union, Optional

from models import Symbol, Category, Pin

logger = logging.getLogger(__name__)

class AssetFormat:
    """Standardized keys for the asset dictionary."""
    SYMBOL = 'symbol'
    FOOTPRINT = 'footprint'
    MODEL = 'model'
    DATASHEET = 'datasheet'
    
    # Internal tags for third-party files before conversion
    ALTIUM_SYM = 'altium_sym'
    ALTIUM_FP = 'altium_fp'
    EASYEDA_SYM = 'easyeda_sym'
    EASYEDA_FP = 'easyeda_fp'

class FileImporter:
    @staticmethod
    def sanitize_name(name: str) -> str:
        """Universal helper to ensure part names translate safely to file paths."""
        s = str(name).strip()
        s = re.sub(r'[<>:"/\\|?*\s]', '_', s)
        s = re.sub(r'_+', '_', s)
        return s or "Unknown_Part"

    @staticmethod
    def process_single_asset(file_path: Union[str, Path], tmp_dir: Path) -> str:
        """Processes and converts a single 3rd party asset into a native KiCad file."""
        p = Path(file_path)
        # Note: If adding Altium/EasyEDA Python converters later, hook them in here!
        return str(p)

    @staticmethod
    def extract_and_scan(file_paths: List[Union[str, Path]], tmp_dir: Path) -> Dict[str, List[Path]]:
        """
        Extracts archives and scans loose files to identify KiCad assets.
        Auto-downloads datasheets if found inside vendor JSON descriptors.
        """
        final_assets: Dict[str, List[Path]] = {
            AssetFormat.SYMBOL: [],
            AssetFormat.FOOTPRINT: [],
            AssetFormat.MODEL: [],
            AssetFormat.DATASHEET: []
        }
        
        extracted_files: List[Path] = []
        
        # 1. Unpack ZIPs or stage loose files
        for f in file_paths:
            p = Path(f)
            if p.suffix.lower() in ['.zip', '.elibz']:
                try:
                    with zipfile.ZipFile(p, 'r') as zip_ref:
                        zip_ref.extractall(tmp_dir)
                        for root, _, files in os.walk(tmp_dir):
                            for file in files:
                                extracted_files.append(Path(root) / file)
                except Exception as e:
                    logger.error(f"Failed to extract {p.name}: {e}")
            else:
                extracted_files.append(p)
                
        # 2. Sniff file extensions to categorize assets
        for p in extracted_files:
            if p.is_file():
                ext = p.suffix.lower()
                if ext == '.kicad_sym':
                    final_assets[AssetFormat.SYMBOL].append(p)
                elif ext == '.kicad_mod':
                    final_assets[AssetFormat.FOOTPRINT].append(p)
                elif ext in ['.step', '.stp', '.wrl', '.stl']:
                    final_assets[AssetFormat.MODEL].append(p)
                elif ext == '.pdf':
                    final_assets[AssetFormat.DATASHEET].append(p)
                elif ext == '.json':
                    # 3. Scrape JSON files for datasheets (like device.json from some vendors)
                    try:
                        with open(p, 'r', encoding='utf-8') as json_file:
                            data = json.load(json_file)
                            
                            # Search recursively for URLs ending in pdf or datasheet fields
                            def find_datasheet(d: Union[dict, list]) -> Optional[str]:
                                if isinstance(d, dict):
                                    for k, val in d.items():
                                        if isinstance(val, str) and (k.lower() == 'datasheet' or val.lower().endswith('.pdf')):
                                            return val
                                        if isinstance(val, (dict, list)):
                                            res = find_datasheet(val)
                                            if res: return res
                                elif isinstance(d, list):
                                    for item in d:
                                        if isinstance(item, (dict, list)):
                                            res = find_datasheet(item)
                                            if res: return res
                                return None
                            
                            ds_url = find_datasheet(data)
                            if ds_url:
                                # Handle protocol-relative URLs in vendor JSON files
                                if ds_url.startswith('//'):
                                    ds_url = "https:" + ds_url
                                    
                                dest_pdf = tmp_dir / f"{p.stem}_datasheet.pdf"
                                try:
                                    req = urllib.request.Request(ds_url, headers={'User-Agent': 'Mozilla/5.0'})
                                    with urllib.request.urlopen(req, timeout=10) as response:
                                        # Securely confirm it actually returned a PDF
                                        if 'application/pdf' in response.headers.get('Content-Type', '').lower() or ds_url.lower().endswith('.pdf'):
                                            with open(dest_pdf, 'wb') as pdf_file:
                                                pdf_file.write(response.read())
                                            final_assets[AssetFormat.DATASHEET].append(dest_pdf)
                                            logger.info("Datasheet downloaded successfully.")
                                except Exception as e:
                                    logger.error(f"Datasheet download failed: {e}")
                    except Exception as e:
                        logger.error(f"JSON parsing failed: {e}")
                        
        return final_assets

    @staticmethod
    def find_legacy_part_dirs(root_folder: Union[str, Path]) -> List[Path]:
        """
        Scans a root folder and returns a list of Path objects for directories 
        that contain at least one recognized component file.
        """
        part_dirs = set()
        valid_exts = {'.kicad_sym', '.elibz', '.schdoc', '.esym'}
        
        for root, _, files in os.walk(str(root_folder)):
            for f in files:
                if Path(f).suffix.lower() in valid_exts:
                    part_dirs.add(Path(root))
                    break
        return sorted(list(part_dirs))
    

class KiCadIO:
    """Strict I/O handler and validator for KiCad S-expressions."""
    
    @staticmethod
    def backup_file(filepath: Path, max_backups: int = 5, backup_window_minutes: int = 5, lib_root: Optional[Path] = None):
        """Creates a rotating backup within a centralized `.bak` folder mimicking the library hierarchy."""
        if not filepath.exists() or max_backups <= 0:
            return

        # Try to heuristically determine the library root if not provided
        if not lib_root:
            parts = filepath.parts
            for known_dir in ["Symbols", "Footprints", "3D_Models", "Datasheets", "Images"]:
                if known_dir in parts:
                    idx = parts.index(known_dir)
                    lib_root = Path(*parts[:idx])
                    break
        
        if not lib_root:
            lib_root = filepath.parent

        # Compute relative path to preserve hierarchy inside .bak
        try:
            rel_path = filepath.relative_to(lib_root)
        except ValueError:
            rel_path = Path(filepath.name)
            
        bak_dir = lib_root / ".bak" / rel_path.parent
        bak_dir.mkdir(parents=True, exist_ok=True)

        # Uses filepath.name directly to preserve the original extension (e.g., .kicad_mod.bak_*)
        pattern = f"{filepath.name}.bak_*"
        existing_backups = list(bak_dir.glob(pattern))
        existing_backups.sort(key=lambda p: p.name)

        now = datetime.datetime.now()
        create_new = True
        
        if existing_backups:
            newest_backup = existing_backups[-1]
            mtime = datetime.datetime.fromtimestamp(newest_backup.stat().st_mtime)
            delta_minutes = (now - mtime).total_seconds() / 60.0
            
            if delta_minutes <= backup_window_minutes:
                shutil.copy2(filepath, newest_backup)
                logger.info(f"Safety First: Updated existing backup: {newest_backup.name}")
                create_new = False
        
        if create_new:
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            backup_path = bak_dir / f"{filepath.name}.bak_{timestamp}"
            shutil.copy2(filepath, backup_path)
            existing_backups.append(backup_path)
            logger.info(f"Safety First: Created new backup: {backup_path.name}")
            
        while len(existing_backups) > max_backups:
            oldest_backup = existing_backups.pop(0)
            try:
                oldest_backup.unlink()
                logger.info(f"Cleaned up old backup: {oldest_backup.name}")
            except OSError as e:
                logger.warning(f"Failed to delete old backup {oldest_backup.name}: {e}")

    @classmethod
    def _fix_unescaped_quotes(cls, content: str) -> str:
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if '(property "' in line:
                parts = line.split('"', 3)
                if len(parts) >= 4:
                    prefix = '"'.join(parts[:3]) + '"'
                    remainder = parts[3]
                    
                    end_marker_idx = remainder.rfind('" (')
                    if end_marker_idx == -1:
                        end_marker_idx = remainder.rfind('")')
                        
                    if end_marker_idx != -1:
                        val = remainder[:end_marker_idx]
                        suffix = remainder[end_marker_idx:]
                        
                        val = val.replace('\\"', '"').replace('"', '\\"')
                        val = re.sub(r'\s+', ' ', val.replace('\\n', ' ').replace('\\r', ' ')).strip()
                        lines[i] = prefix + val + suffix
        return '\n'.join(lines)

    @classmethod
    def _isolate_blocks(cls, content: str) -> List[str]:
        blocks = []
        depth = 0
        in_string = False
        escape_next = False
        start_idx = -1

        for i, char in enumerate(content):
            if escape_next:
                escape_next = False
                continue

            if char == '\\':
                escape_next = True
                continue

            if char == '"':
                in_string = not in_string
                continue

            if not in_string:
                if char == '(':
                    depth += 1
                    if depth == 2 and content[i+1:i+7] == "symbol":
                        start_idx = i
                elif char == ')':
                    if depth == 2 and start_idx != -1:
                        blocks.append(content[start_idx:i+1])
                        start_idx = -1
                    depth -= 1
                    
        if start_idx != -1 and depth > 1:
            missing_parens = depth - 1
            healed_block = content[start_idx:] + ("\n" + ")" * missing_parens)
            blocks.append(healed_block)
            logger.warning(f"Parser healed a severely corrupted block missing {missing_parens} closing parentheses.")

        return blocks

    @classmethod
    def parse_category(cls, filepath: Path) -> Optional[Category]:
        if not filepath.exists() or not filepath.is_file():
            return None

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        content = cls._fix_unescaped_quotes(content)
        blocks = cls._isolate_blocks(content)

        category = Category(name=filepath.stem, filepath=filepath)

        for block in blocks:
            name_match = re.search(r'^\(symbol\s+"([^"]+)"', block)
            if not name_match:
                continue

            sym = Symbol(name=name_match.group(1), category=category.name)
            sym.raw_graphics_block = block

            prop_pattern = r'\(property\s+"([^"]+)"\s+"((?:[^"\\]|\\.)*)"'
            for key, val in re.findall(prop_pattern, block):
                sym.properties[key] = val.replace('\\"', '"').replace('\\\\', '\\')

            pin_pattern = r'\(pin\s+(?P<dir>[^\s]+)\s+(?P<style>[^\s]+)\s+\(at\s+(?P<at>[^\)]+)\).*?\(name\s+(?P<name>"(?:[^"\\]|\\.)*"|~).*?\(number\s+(?P<num>"(?:[^"\\]|\\.)*"|~)'
            for m in re.finditer(pin_pattern, block, re.DOTALL):
                sym.pins.append(Pin(
                    number=m.group('num').replace('"', ''),
                    name=m.group('name').replace('"', ''),
                    direction=m.group('dir'),
                    style=m.group('style'),
                    at=m.group('at')
                ))

            category.add_symbol(sym)

        return category

    @classmethod
    def rename_symbol_in_block(cls, block: str, old_name: str, new_name: str) -> str:
        """Safely renames the internal root components of a raw graphical block. Required by main.py."""
        safe_new = new_name.replace('\\', '\\\\').replace('"', '\\"')
        safe_old = re.escape(old_name)

        pattern = r'(symbol\s+")' + safe_old + r'((?:_(?:[^"\\]|\\.)+)?")'
        block = re.sub(pattern, r'\g<1>' + safe_new + r'\g<2>', block)

        val_pattern = r'(\(property\s+"Value"\s+")' + safe_old + r'(")'
        block = re.sub(val_pattern, r'\g<1>' + safe_new + r'\g<2>', block)

        return block

    @classmethod
    def _strip_and_rebuild_properties(cls, sym: Symbol) -> str:
        block = sym.raw_graphics_block
        
        new_block_chars = []
        i = 0
        while i < len(block):
            if block[i:i+10] == "(property ":
                p_depth = 0
                in_str = False
                escape = False
                j = i
                while j < len(block):
                    c = block[j]
                    if escape: escape = False
                    elif c == '\\': escape = True
                    elif c == '"': in_str = not in_str
                    elif not in_str:
                        if c == '(': p_depth += 1
                        elif c == ')': 
                            p_depth -= 1
                            if p_depth == 0:
                                j += 1
                                break
                    j += 1
                i = j
                while i < len(block) and block[i] in ' \t\r\n':
                    i += 1
                continue
                
            new_block_chars.append(block[i])
            i += 1
            
        stripped_block = "".join(new_block_chars)
        
        ordered_keys = ["Reference", "Value", "Footprint", "Datasheet"]
        other_keys = sorted([k for k in sym.properties.keys() if k not in ordered_keys])
        
        props_out = []
        for index, key in enumerate(ordered_keys + other_keys):
            val = sym.properties.get(key, "")
            val = str(val).replace('\n', ' ').replace('\r', '').replace('\\', '\\\\').replace('"', '\\"')
            
            if key == "Reference":
                effects = "(effects (font (size 1.27 1.27)) (justify left bottom))"
                at = "(at -5.08 6.35 0)"
            elif key == "Value":
                effects = "(effects (font (size 1.27 1.27)) (justify left bottom))"
                at = "(at -5.08 -7.62 0)"
            else:
                effects = "(effects (font (size 1.27 1.27)) hide)"
                at = "(at 0 0 0)"
                
            prop_str = f'  (property "{key}" "{val}" (id {index}) {at}\n    {effects}\n  )'
            props_out.append(prop_str)
            
        props_text = "\n".join(props_out)
        
        first_newline = stripped_block.find('\n')
        if first_newline != -1:
            final_block = stripped_block[:first_newline+1] + props_text + "\n" + stripped_block[first_newline+1:]
        else:
            final_block = stripped_block + "\n" + props_text
            
        final_block = re.sub(r'\n\s*\n', '\n', final_block)
        return final_block

    @classmethod
    def write_category(cls, category: Category, max_backups: int = 5, backup_window: int = 5) -> bool:
        if category.filepath.exists():
            cls.backup_file(category.filepath, max_backups, backup_window)

        lines = [
            "(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor)"
        ]

        # Topological sort to handle `(extends ...)` dependency ordering.
        # KiCad requires parent symbols to be written before their children in the file.
        ordered_syms = []
        visited = set()
        sym_dict = {s.name: s for s in category.symbols}

        def visit(sym_name):
            if sym_name in visited:
                return
            
            if sym_name in sym_dict:
                # Find if this symbol extends another symbol
                m = re.search(r'\(extends\s+"([^"]+)"\)', sym_dict[sym_name].raw_graphics_block)
                if m:
                    parent_name = m.group(1)
                    # If parent is also in this file, we must write the parent first
                    if parent_name in sym_dict:
                        visit(parent_name)
            
            visited.add(sym_name)
            if sym_name in sym_dict:
                ordered_syms.append(sym_dict[sym_name])

        # Process alphabetically, but `visit` logic ensures parent dependencies are hoisted first
        for sym in sorted(category.symbols, key=lambda s: s.name):
            visit(sym.name)

        for sym in ordered_syms:
            block = cls._strip_and_rebuild_properties(sym)
            indented_block = "\n".join("  " + line if line else "" for line in block.split("\n"))
            lines.append(indented_block)

        lines.append(")\n")

        try:
            category.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(category.filepath, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines))
            return True
        except IOError as e:
            logger.error(f"Failed to write category {category.name} to disk: {e}")
            return False
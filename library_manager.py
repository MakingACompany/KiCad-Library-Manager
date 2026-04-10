"""
library_manager.py
Consolidates core file management and library health scanning logic.
Acts as the Controller for the in-memory Library object (MVC pattern).
"""
import os
import logging
import re
import shutil
import platform
import urllib.request
import json
from pathlib import Path
from typing import List, Dict, Optional

from PySide6.QtWidgets import (QDialog, QMessageBox, QTreeWidgetItem, 
                               QApplication, QFileDialog, QTreeWidget, QLineEdit)
from PySide6.QtCore import Qt, QSettings, QThread, Signal, QObject, QTimer

from models import Library, Symbol, Category
from api import DigiKeyAPI
from file_io import FileImporter, KiCadIO
from ui_views import (SymbolEditorDialog, MergeComparisonDialog, 
                      OrphanResolverDialog, LinkFilesDialog, 
                      BackupRestoreDialog, PartSelectionDialog,
                      get_library_prefix, get_setting_int, get_setting_bool,
                      get_setting_str, get_active_categories)

logger = logging.getLogger(__name__)

# ==========================================
# CUSTOM WIDGETS & THREADS
# ==========================================

class LibraryScannerThread(QThread):
    scan_completed = Signal(object)
    
    def __init__(self, lib_root: Optional[Path]):
        super().__init__()
        self.lib_root = lib_root
        
    def run(self):
        lib = Library()
        if self.lib_root is not None:
            lib.set_root_path(str(self.lib_root))
            sym_dir = lib.get_symbols_dir()
            
            if sym_dir is not None and sym_dir.exists():
                for sym_file in sym_dir.glob("*.kicad_sym"):
                    cat = KiCadIO.parse_category(sym_file)
                    if cat:
                        lib.categories[cat.name] = cat
                    
        self.scan_completed.emit(lib)

# ==========================================
# UTILITY CLASSES
# ==========================================

class AssetManager:
    @staticmethod
    def get_path_string(settings: QSettings, lib_root: Path, absolute_path: Path) -> str:
        """Determines the correct string to inject (Variable vs Absolute) for Models and Datasheets."""
        path_var = get_setting_str(settings, "kicad_path_var", "").strip()
        if path_var:
            try:
                rel_path = absolute_path.relative_to(lib_root)
                return f"{path_var}/{rel_path}".replace('\\', '/')
            except ValueError:
                pass
        return str(absolute_path).replace('\\', '/')

    @staticmethod
    def smart_rename_assets(old_name: str, new_name: str, old_sym: Symbol, new_cat_full: str, new_props: dict, lib_root: Optional[Path]):
        """
        Dynamically orchestrates physical renames and category moves of linked footprints, 
        datasheets, and 3D models. Actively updates internal links to prevent breakage.
        """
        if lib_root is None or not lib_root.exists():
            return

        old_cat_clean = old_sym.category.split('_', 1)[-1] if '_' in old_sym.category else old_sym.category
        new_cat_clean = new_cat_full.split('_', 1)[-1] if '_' in new_cat_full else new_cat_full

        if old_name == new_name and old_cat_clean == new_cat_clean:
            return

        settings = QSettings("OpenSourceTools", "KiCadLibManager")
        path_var_raw = get_setting_str(settings, "kicad_path_var", "").strip()

        # 1. FOOTPRINT & 3D MODEL (via footprint)
        old_fp_ref = old_sym.get_clean_property("Footprint", "")
        if old_fp_ref and ":" in old_fp_ref:
            cat_dir, fp_file = old_fp_ref.split(":", 1)
            old_fp_path = lib_root / "Footprints" / f"{cat_dir}.pretty" / f"{fp_file}.kicad_mod"
            
            if old_fp_path.exists() and old_fp_path.is_file():
                prefix = cat_dir[:len(cat_dir)-len(old_cat_clean)] if cat_dir.endswith(old_cat_clean) else ""
                new_cat_dir = f"{prefix}{new_cat_clean}"
                
                new_fp_dir = lib_root / "Footprints" / f"{new_cat_dir}.pretty"
                new_fp_dir.mkdir(parents=True, exist_ok=True)
                new_fp_path = new_fp_dir / f"{new_name}.kicad_mod"
                
                if old_fp_path.resolve() != new_fp_path.resolve():
                    shutil.move(str(old_fp_path), str(new_fp_path))
                    
                current_ui_fp = new_props.get("Footprint", "")
                if not current_ui_fp or current_ui_fp == old_fp_ref:
                    new_props["Footprint"] = f"{new_cat_dir}:{new_name}"
                    
                try:
                    with open(new_fp_path, 'r', encoding='utf-8') as f:
                        fp_content = f.read()
                        
                    m_match = re.search(r'\(model\s+"([^"]+)"', fp_content)
                    if m_match:
                        old_m_str = m_match.group(1)
                        old_m_path = Path(old_m_str)
                        
                        actual_old_mdl = lib_root / "3D_Models" / old_cat_clean / old_m_path.name
                        if not actual_old_mdl.exists():
                            clean_m_str = old_m_str.replace("${KIPRJMOD}/", "").replace("${KICAD6_3DMODEL_DIR}/", "").replace("${KICAD7_3DMODEL_DIR}/", "")
                            actual_old_mdl = lib_root / clean_m_str
                            
                        if actual_old_mdl.exists():
                            new_m_name = f"{new_name}{actual_old_mdl.suffix}"
                            new_m_dir = lib_root / "3D_Models" / new_cat_clean
                            new_m_dir.mkdir(parents=True, exist_ok=True)
                            new_m_path = new_m_dir / new_m_name
                            
                            if actual_old_mdl.resolve() != new_m_path.resolve():
                                shutil.move(str(actual_old_mdl), str(new_m_path))
                                
                            mdl_path_str = AssetManager.get_path_string(settings, lib_root, new_m_path)
                            fp_content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', fp_content)
                            with open(new_fp_path, 'w', encoding='utf-8') as f:
                                f.write(fp_content)
                                
                            old_mdl_prop = old_sym.get_clean_property("3D_Model", "")
                            if old_mdl_prop and (not new_props.get("3D_Model") or new_props.get("3D_Model") == old_mdl_prop):
                                new_props["3D_Model"] = AssetManager.get_path_string(settings, lib_root, new_m_path)
                except Exception as e:
                    logger.error(f"Failed to rename model inside footprint: {e}")

        # 2. DATASHEET
        old_ds_ref = old_sym.get_clean_property("Datasheet", "")
        if old_ds_ref and not str(old_ds_ref).startswith("http"):
            old_ds_path = Path(old_ds_ref)
            if not old_ds_path.is_absolute():
                old_ds_path = lib_root / old_ds_path
                
            if old_ds_path.exists() and lib_root in old_ds_path.parents:
                new_ds_dir = lib_root / "Datasheets" / new_cat_clean
                new_ds_dir.mkdir(parents=True, exist_ok=True)
                new_ds_path = new_ds_dir / f"{new_name}{old_ds_path.suffix}"
                
                if old_ds_path.resolve() != new_ds_path.resolve():
                    shutil.move(str(old_ds_path), str(new_ds_path))
                    
                current_ui_ds = new_props.get("Datasheet", "")
                if not current_ui_ds or current_ui_ds == old_ds_ref or Path(current_ui_ds).resolve() == old_ds_path.resolve():
                    new_props["Datasheet"] = AssetManager.get_path_string(settings, lib_root, new_ds_path)

        # 3. IMAGE
        old_img_ref = old_sym.get_clean_property("Image_File", "")
        if old_img_ref and not str(old_img_ref).startswith("http"):
            old_img_path = Path(old_img_ref)
            if not old_img_path.is_absolute():
                old_img_path = lib_root / old_img_path
                
            if old_img_path.exists() and lib_root in old_img_path.parents:
                new_img_dir = lib_root / "Images" / new_cat_clean
                new_img_dir.mkdir(parents=True, exist_ok=True)
                new_img_path = new_img_dir / f"{new_name}{old_img_path.suffix}"
                
                if old_img_path.resolve() != new_img_path.resolve():
                    shutil.move(str(old_img_path), str(new_img_path))
                    
                current_ui_img = new_props.get("Image_File", "")
                if not current_ui_img or current_ui_img == old_img_ref or Path(current_ui_img).resolve() == old_img_path.resolve():
                    try:
                        rel_img = new_img_path.relative_to(lib_root)
                        new_props["Image_File"] = str(rel_img).replace('\\', '/')
                    except ValueError: pass


class LibraryScanner:
    @staticmethod
    def find_issues(library: Library, lib_root: Optional[Path], prefix: str) -> List[Dict]:
        """
        Scans the entire library and explicitly flags duplicates, orphaned physical files, 
        broken links, and assets placed in the wrong category folders.
        """
        all_parts = library.get_all_symbols()
        issues = []
        processed_uuids = set()

        settings = QSettings("OpenSourceTools", "KiCadLibManager")
        path_var_raw = get_setting_str(settings, "kicad_path_var", "").strip()

        valid_exact_names = {sym.name for sym in all_parts}
        def is_alternate_asset(filename_stem: str) -> bool:
            if filename_stem in valid_exact_names: return True
            for sym_name in valid_exact_names:
                if filename_stem.startswith(sym_name + "_") or filename_stem.startswith(sym_name + "-"):
                    return True
            return False

        # 1. Component Duplication Scanning
        for i, p1 in enumerate(all_parts):
            if p1.uuid in processed_uuids: continue

            group = [p1]
            processed_uuids.add(p1.uuid)
            reasons = set()

            for j, p2 in enumerate(all_parts):
                if p2.uuid in processed_uuids: continue

                match = []
                if p1.name == p2.name: 
                    match.append("Exact Name Match")
                
                p1_mpn = p1.get_clean_property('MPN').strip().lower()
                p2_mpn = p2.get_clean_property('MPN').strip().lower()
                if p1_mpn == p2_mpn and p1_mpn not in ['none', 'n/a', '-', '']: 
                    match.append(f"Same MPN ({p1.get_clean_property('MPN')})")
                
                p1_dk = p1.get_clean_property('DigiKey_PN', p1.get_clean_property('Supplier Part')).strip().lower()
                p2_dk = p2.get_clean_property('DigiKey_PN', p2.get_clean_property('Supplier Part')).strip().lower()
                if p1_dk == p2_dk and p1_dk not in ['none', 'n/a', '-', '']: 
                    match.append(f"Same Supplier PN ({p1.get_clean_property('DigiKey_PN', p1.get_clean_property('Supplier Part'))})")

                if match:
                    group.append(p2)
                    processed_uuids.add(p2.uuid)
                    reasons.update(match)

            if len(group) > 1:
                group.sort(key=lambda x: x.name)
                parts_digest = []
                for s in group:
                    cat_obj = library.categories.get(s.category)
                    f_path = cat_obj.filepath if cat_obj else None
                    parts_digest.append({'name': s.name, 'category': s.category, 'sym_obj': s, 'props': s.properties, 'file': f_path})
                issues.append({'type': 'Duplicate / Conflict Group', 'desc': " | ".join(sorted(reasons)), 'parts': parts_digest})

        if not lib_root or not lib_root.exists(): return issues

        # 2. File Link Validation & Orphan Hunting
        fp_root = lib_root / "Footprints"
        mdl_root = lib_root / "3D_Models"
        ds_root = lib_root / "Datasheets"
        img_root = lib_root / "Images"

        used_fps = set()
        used_mdls = set()
        used_dss = set()
        used_imgs = set()

        for sym in all_parts:
            cat_name = sym.category
            cat_clean = cat_name[len(prefix):] if cat_name.startswith(prefix) else cat_name
            cat_obj = library.categories.get(sym.category)
            sym_filepath = cat_obj.filepath if cat_obj else None
            
            # --- Check Footprint ---
            fp_str = sym.get_clean_property("Footprint", "")
            if fp_str and ":" in fp_str:
                f_cat, f_name = fp_str.split(":", 1)
                fp_path = fp_root / f"{f_cat}.pretty" / f"{f_name}.kicad_mod"
                if not fp_path.exists():
                    issues.append({'type': 'Broken Link', 'desc': f"Broken Footprint Link: {fp_str}", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'broken_fp', 'sym_obj': sym, 'ref': fp_str, 'file': sym_filepath}]})
                else:
                    used_fps.add(fp_path.resolve())
                    if f_cat != cat_clean and f_cat != f"{prefix}{cat_clean}":
                        issues.append({'type': 'Wrong Category', 'desc': f"Misplaced Footprint (Currently in '{f_cat}')", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'wrong_cat_fp', 'sym_obj': sym, 'path': fp_path, 'expected_cat': cat_clean, 'file': sym_filepath}]})
                        
                    # Check 3D Model specifically embedded inside the Footprint
                    try:
                        with open(fp_path, 'r', encoding='utf-8') as f: content = f.read()
                        m_match = re.search(r'\(model\s+"([^"]+)"', content)
                        if m_match:
                            m_path_str = m_match.group(1)
                            if m_path_str:
                                clean_m_str = m_path_str.replace("${KIPRJMOD}/", "").replace("${KICAD6_3DMODEL_DIR}/", "").replace("${KICAD7_3DMODEL_DIR}/", "")
                                if path_var_raw: clean_m_str = clean_m_str.replace(f"{path_var_raw}/", "").replace(path_var_raw, "")
                                m_path = Path(clean_m_str)
                                if not m_path.is_absolute() and lib_root: m_path = lib_root / m_path
                                    
                                if not m_path.exists():
                                    local_attempt = mdl_root / cat_clean / m_path.name
                                    if local_attempt.exists(): m_path = local_attempt
                                        
                                if not m_path.exists():
                                    issues.append({'type': 'Broken Link', 'desc': f"Broken 3D Model (in Footprint): {m_path.name}", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'broken_fp_mdl', 'sym_obj': sym, 'fp_path': fp_path, 'file': sym_filepath}]})
                                else:
                                    used_mdls.add(m_path.resolve())
                                    if lib_root in m_path.parents:
                                        try:
                                            rel = m_path.relative_to(mdl_root).parts
                                            if rel and rel[0] != cat_clean:
                                                issues.append({'type': 'Wrong Category', 'desc': f"Misplaced 3D Model (Currently in '{rel[0]}')", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'wrong_cat_mdl', 'sym_obj': sym, 'path': m_path, 'expected_cat': cat_clean, 'fp_path': fp_path, 'file': sym_filepath}]})
                                        except ValueError: pass
                    except Exception: pass

            # --- Check 3D Model (Base Properties) ---
            sym_mdl_str = sym.get_clean_property("3D_Model", sym.get_clean_property("3D Model", ""))
            if sym_mdl_str:
                sym_mdl_clean = str(sym_mdl_str)
                if path_var_raw and sym_mdl_clean.startswith(path_var_raw):
                    sym_mdl_clean = sym_mdl_clean.replace(path_var_raw, "").lstrip("\\/")
                elif sym_mdl_clean.startswith("${KIPRJMOD}"):
                    sym_mdl_clean = sym_mdl_clean.replace("${KIPRJMOD}", "").lstrip("\\/")
                    
                sym_mdl_path = Path(sym_mdl_clean)
                if not sym_mdl_path.is_absolute() and lib_root: sym_mdl_path = lib_root / sym_mdl_path
                    
                if not sym_mdl_path.exists():
                    issues.append({'type': 'Broken Link', 'desc': f"Broken 3D Model (Property): {sym_mdl_path.name}", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'broken_mdl_prop', 'sym_obj': sym, 'file': sym_filepath}]})
                else:
                    used_mdls.add(sym_mdl_path.resolve())
                    try:
                        if lib_root in sym_mdl_path.parents:
                            rel = sym_mdl_path.relative_to(mdl_root).parts
                            if rel and rel[0] != cat_clean:
                                issues.append({'type': 'Wrong Category', 'desc': f"Misplaced 3D Model (Currently in '{rel[0]}')", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'wrong_cat_mdl', 'sym_obj': sym, 'path': sym_mdl_path, 'expected_cat': cat_clean, 'file': sym_filepath}]})
                    except ValueError: pass

            # --- Check Datasheet ---
            ds_str = sym.get_clean_property("Datasheet", "")
            if ds_str and not str(ds_str).startswith("http"):
                ds_clean = str(ds_str)
                if path_var_raw and ds_clean.startswith(path_var_raw): ds_clean = ds_clean.replace(path_var_raw, "").lstrip("\\/")
                    
                ds_path = Path(ds_clean)
                if not ds_path.is_absolute() and lib_root: ds_path = lib_root / ds_path
                    
                if not ds_path.exists():
                    issues.append({'type': 'Broken Link', 'desc': f"Broken Datasheet Link: {ds_path.name}", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'broken_ds', 'sym_obj': sym, 'file': sym_filepath}]})
                else:
                    used_dss.add(ds_path.resolve())
                    try:
                        if lib_root in ds_path.parents:
                            rel = ds_path.relative_to(ds_root).parts
                            if rel and rel[0] != cat_clean:
                                issues.append({'type': 'Wrong Category', 'desc': f"Misplaced Datasheet (Currently in '{rel[0]}')", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'wrong_cat_ds', 'sym_obj': sym, 'path': ds_path, 'expected_cat': cat_clean, 'file': sym_filepath}]})
                    except ValueError: pass

            # --- Check Image ---
            img_str = sym.get_clean_property("Image_File", "")
            if img_str and not str(img_str).startswith("http"):
                img_clean = str(img_str)
                if path_var_raw and img_clean.startswith(path_var_raw): img_clean = img_clean.replace(path_var_raw, "").lstrip("\\/")
                    
                img_path = Path(img_clean)
                if not img_path.is_absolute() and lib_root: img_path = lib_root / img_path
                    
                if not img_path.exists():
                    issues.append({'type': 'Broken Link', 'desc': f"Broken Image Link: {img_path.name}", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'broken_img', 'sym_obj': sym, 'file': sym_filepath}]})
                else:
                    used_imgs.add(img_path.resolve())
                    try:
                        if lib_root in img_path.parents:
                            rel = img_path.relative_to(img_root).parts
                            if rel and rel[0] != cat_clean:
                                issues.append({'type': 'Wrong Category', 'desc': f"Misplaced Image (Currently in '{rel[0]}')", 'parts': [{'name': sym.name, 'category': sym.category, 'issue_type': 'wrong_cat_img', 'sym_obj': sym, 'path': img_path, 'expected_cat': cat_clean, 'file': sym_filepath}]})
                    except ValueError: pass

        # ----------------------------------------
        # Orphan Hunting
        # ----------------------------------------
        if fp_root.exists():
            for f in fp_root.rglob("*.kicad_mod"):
                if '.bak' in f.parts: continue
                if f.resolve() in used_fps: continue
                if is_alternate_asset(f.stem): used_fps.add(f.resolve())
                else: issues.append({'type': 'Orphaned Asset', 'desc': f"Unused Footprint: {f.name}", 'parts': [{'name': f.name, 'category': f.parent.name.replace('.pretty',''), 'issue_type': 'orphan_fp', 'path': f, 'file': None}]})

        if mdl_root.exists():
            for f in mdl_root.rglob("*"):
                if f.is_file() and f.suffix.lower() in ['.step', '.stp', '.wrl']:
                    if '.bak' in f.parts: continue
                    if f.resolve() in used_mdls: continue
                    if is_alternate_asset(f.stem): used_mdls.add(f.resolve())
                    else: issues.append({'type': 'Orphaned Asset', 'desc': f"Unused 3D Model: {f.name}", 'parts': [{'name': f.name, 'category': f.parent.name, 'issue_type': 'orphan_mdl', 'path': f, 'file': None}]})
                    
        if ds_root.exists():
            for f in ds_root.rglob("*.pdf"):
                if '.bak' in f.parts: continue
                if f.resolve() in used_dss: continue
                if is_alternate_asset(f.stem): used_dss.add(f.resolve())
                else: issues.append({'type': 'Orphaned Asset', 'desc': f"Unused Datasheet: {f.name}", 'parts': [{'name': f.name, 'category': f.parent.name, 'issue_type': 'orphan_ds', 'path': f, 'file': None}]})

        if img_root.exists():
            for f in img_root.rglob("*"):
                if f.is_file() and f.suffix.lower() in ['.png', '.jpg', '.jpeg']:
                    if '.bak' in f.parts: continue
                    if f.resolve() in used_imgs: continue
                    if is_alternate_asset(f.stem): used_imgs.add(f.resolve())
                    else: issues.append({'type': 'Orphaned Asset', 'desc': f"Unused Image: {f.name}", 'parts': [{'name': f.name, 'category': f.parent.name, 'issue_type': 'orphan_img', 'path': f, 'file': None}]})

        return issues


# ==========================================
# MAIN CONTROLLER CLASS
# ==========================================

class LibraryController(QObject):
    """
    The central Controller module. Evaluates logic, performs physical file I/O operations, 
    and bridges interactions between the UI (Main Window) and the models (Library).
    """
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.settings = QSettings("OpenSourceTools", "KiCadLibManager")
        self.tmp_folder = Path(os.getcwd()) / ".tmp"
        
        self.app_library = Library()
        self.cached_part_names = set()
        self.dk_api = DigiKeyAPI()
        
        self.selected_symbols: List[Symbol] = []
        self.selected_issues: List[Dict] = []

    def get_active_library_root(self, show_warning: bool = False) -> Optional[Path]:
        if self.app_library.is_valid():
            return self.app_library.root_path
        if show_warning:
            QMessageBox.warning(self.view, "Setup Required", "Please select your KiCad Library Root folder first.")
            self.view.open_settings()
            if self.app_library.is_valid():
                return self.app_library.root_path
        return None

    def check_startup(self):
        saved_path = get_setting_str(self.settings, "library_root", "")
        if saved_path and Path(saved_path).exists():
            self.app_library.set_root_path(saved_path)
            self.reload_library(preserve_state=False)
        else:
            QMessageBox.warning(self.view, "Library Not Found", "No KiCad Library Root folder has been configured yet.")
            self.view.open_settings()

    def reload_library(self, preserve_state=False):
        if not self.app_library.is_valid(): return

        self._preserve_state_flag = preserve_state
        if preserve_state:
            self._expanded_browser = set()
            self._selected_browser = set()
            self._expanded_health = set()
            self._selected_health = set()
            
            for i in range(self.view.browser_tree.topLevelItemCount()):
                item = self.view.browser_tree.topLevelItem(i)
                if item is not None:
                    if item.isExpanded(): self._expanded_browser.add(item.text(0))
                    for j in range(item.childCount()):
                        child_item = item.child(j)
                        if child_item is not None and child_item.isSelected():
                            sym = child_item.data(0, Qt.ItemDataRole.UserRole)
                            if sym and hasattr(sym, 'uuid'): self._selected_browser.add(sym.uuid)
            
            for i in range(self.view.health_tree.topLevelItemCount()):
                item = self.view.health_tree.topLevelItem(i)
                if item is not None:
                    if item.isExpanded(): self._expanded_health.add(item.text(0))
                    for j in range(item.childCount()):
                        child_item = item.child(j)
                        if child_item is not None and child_item.isSelected():
                            sym = child_item.data(0, Qt.ItemDataRole.UserRole)
                            if sym and isinstance(sym, dict) and '_fixer_id' in sym:
                                self._selected_health.add(sym['_fixer_id'])

        self.view.set_working(True, "Scanning Library...")
        self.view.refresh_btn.setText("Loading...")
        self.view.refresh_btn.setEnabled(False)
        QApplication.processEvents()
        
        lib_root = self.app_library.root_path
        self.scanner_thread = LibraryScannerThread(lib_root)
        self.scanner_thread.scan_completed.connect(self.on_scan_completed)
        self.scanner_thread.start()

    def on_scan_completed(self, loaded_library: Library):
        self.app_library = loaded_library
        self.view.app_library = loaded_library
        self.cached_part_names = {s.name for s in self.app_library.get_all_symbols()}
        self.view.cached_part_names = self.cached_part_names
        
        prefix = get_library_prefix(self.settings)
        self.health_issues = LibraryScanner.find_issues(self.app_library, self.app_library.root_path, prefix)
        
        self.resolve_orphans()
        self.view.populate_health_tree()
        self.view.populate_browser_tree()
        
        if hasattr(self.view, 'populate_report_tree'):
            self.view.populate_report_tree()
        
        if getattr(self, '_preserve_state_flag', False):
            for i in range(self.view.browser_tree.topLevelItemCount()):
                item = self.view.browser_tree.topLevelItem(i)
                if item is not None:
                    if item.text(0) in self._expanded_browser: item.setExpanded(True)
                    for j in range(item.childCount()):
                        child_item = item.child(j)
                        if child_item is not None:
                            sym = child_item.data(0, Qt.ItemDataRole.UserRole)
                            if sym and hasattr(sym, 'uuid') and sym.uuid in self._selected_browser:
                                child_item.setSelected(True)
                                self.view.browser_tree.setCurrentItem(child_item)
                        
            for i in range(self.view.health_tree.topLevelItemCount()):
                item = self.view.health_tree.topLevelItem(i)
                if item is not None:
                    if item.text(0) in self._expanded_health: item.setExpanded(True)
                    for j in range(item.childCount()):
                        child_item = item.child(j)
                        if child_item is not None:
                            sym = child_item.data(0, Qt.ItemDataRole.UserRole)
                            if sym and isinstance(sym, dict) and sym.get('_fixer_id') in self._selected_health:
                                child_item.setSelected(True)
                                self.view.health_tree.setCurrentItem(child_item)
                                
        self.view.refresh_btn.setText("↻ Rescan Library")
        self.view.refresh_btn.setEnabled(True)
        self.view.set_working(False, "Ready")

    def on_selection_changed(self, active_tree: QTreeWidget):
        selected_items = active_tree.selectedItems()
        self.selected_symbols = []
        self.selected_issues = []
        
        for item in selected_items:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data:
                if isinstance(data, Symbol):
                    self.selected_symbols.append(data)
                elif isinstance(data, dict):
                    parts = data.get('parts', [])
                    if 'sym_obj' in data and data['sym_obj']:
                        self.selected_symbols.append(data['sym_obj'])
                    elif parts and 'sym_obj' in parts[0] and parts[0]['sym_obj']:
                        self.selected_symbols.append(parts[0]['sym_obj'])
                        
                    if 'issue_type' in data:
                        self.selected_issues.append(data)
                    elif parts and 'issue_type' in parts[0]:
                        issue_data = parts[0].copy()
                        issue_data['desc'] = data.get('desc', '')
                        self.selected_issues.append(issue_data)
                        
        self.selected_symbols = list({sym.uuid: sym for sym in self.selected_symbols}.values())
        self.view.selected_symbols = self.selected_symbols
        self.view.selected_issues = self.selected_issues
        
        count = len(self.selected_symbols)
        issue_count = len(self.selected_issues)
        
        has_orphan_fp = False
        if issue_count == 1:
            i_type = self.selected_issues[0].get('issue_type', '')
            if i_type in ['orphan_fp', 'wrong_cat_fp']:
                has_orphan_fp = True
                
        can_view_sym = (count == 1)
        can_view_fp = (count == 1) or has_orphan_fp
        
        if count == 0 and issue_count == 0:
            self.view.viewer_panel.clear()
            if self.view.viewer_panel.isVisible():
                self.view.viewer_panel.title_lbl.setText("<b>Preview</b>")
            
        self.view.btn_delete.setEnabled(count > 0 or issue_count > 0)
        self.view.btn_edit.setEnabled(count == 1)
        self.view.btn_view_sym.setEnabled(can_view_sym)
        self.view.btn_view_fp.setEnabled(can_view_fp)
        self.view.btn_resolve.setEnabled(issue_count > 0)
        
        has_path = issue_count == 1 and ('path' in self.selected_issues[0] or 'file' in self.selected_issues[0])
        self.view.btn_open_loc.setEnabled(has_path)
        
        if count >= 2 and issue_count == 0:
            self.view.btn_merge.setEnabled(True)
            self.view.btn_merge.setProperty("action", "success")
            self.view.btn_merge.setText(f"Merge Selected ({count})")
        else:
            self.view.btn_merge.setEnabled(False)
            self.view.btn_merge.setProperty("action", "")
            self.view.btn_merge.setText("Merge Selected")
            
        self.view.btn_merge.style().unpolish(self.view.btn_merge)
        self.view.btn_merge.style().polish(self.view.btn_merge)

        if self.view.viewer_panel.isVisible():
            if "Footprint" in self.view.viewer_panel.title_lbl.text():
                self.view.view_footprint(silent_fail=True)
            else:
                self.view.view_symbol(silent_fail=True)

    def resolve_orphans(self):
        prefix = get_library_prefix(self.settings)
        active_cats = get_active_categories(self.settings)
        valid_names = [f"{prefix}{cat}" for cat in active_cats.keys()] + [f"{prefix}Uncategorized"]
        
        orphans = []
        for cat_name, cat in self.app_library.categories.items():
            if cat_name not in valid_names:
                for sym in cat.symbols:
                    orphans.append({'name': sym.name, 'desc': sym.get_clean_property("Description"), 'file': cat.filepath, 'sym_obj': sym, 'old_cat': cat})
                    
        if orphans:
            dialog = OrphanResolverDialog(self.view, orphans, list(active_cats.keys()))
            if dialog.exec() == int(QDialog.DialogCode.Accepted):
                mapping = dialog.get_mapping()
                self.view.set_working(True, "Remapping orphaned parts...")
                cats_to_save = set()
                
                sym_dir = self.app_library.get_symbols_dir()
                if sym_dir is None: return 
                
                for orphan in orphans:
                    sym = orphan['sym_obj']
                    old_cat = orphan['old_cat']
                    new_cat_raw = mapping.get(sym.name, "Uncategorized")
                    new_cat_name = f"{prefix}{new_cat_raw}"
                    
                    old_cat.remove_symbol_by_name(sym.name)
                    cats_to_save.add(old_cat)
                    
                    sym.category = new_cat_name
                    if new_cat_name not in self.app_library.categories:
                        new_path = sym_dir / f"{new_cat_name}.kicad_sym"
                        self.app_library.categories[new_cat_name] = Category(new_cat_name, new_path)
                        
                    new_cat = self.app_library.categories[new_cat_name]
                    new_cat.add_symbol(sym)
                    cats_to_save.add(new_cat)
                    
                mb = get_setting_int(self.settings, "backups_to_keep", 5)
                bw = get_setting_int(self.settings, "backup_window_minutes", 5)
                
                for cat in cats_to_save:
                    if cat is not None:
                        KiCadIO.write_category(cat, mb, bw)
                        if not cat.symbols and cat.filepath and cat.filepath.exists():
                            try: cat.filepath.unlink()
                            except OSError: pass
                        
                self.view.set_working(False, "Ready")

    def resolve_selected_issues(self):
        if not self.selected_issues: return
        
        lib_root = self.app_library.root_path
        if not lib_root: return
        
        mb = get_setting_int(self.settings, "backups_to_keep", 5)
        bw = get_setting_int(self.settings, "backup_window_minutes", 5)
        prefix = get_library_prefix(self.settings)
        path_var_raw = get_setting_str(self.settings, "kicad_path_var", "").strip()
        
        # --- 1. ORPHAN FILE HANDLING ---
        orphan_issues = [i for i in self.selected_issues if i.get('issue_type', '').startswith('orphan_')]
        for issue in orphan_issues:
            i_type = issue.get('issue_type', '')
            path = issue['path']
            matched_sym = None
            
            for s in self.app_library.get_all_symbols():
                if FileImporter.sanitize_name(s.name).lower() == FileImporter.sanitize_name(path.stem).lower():
                    matched_sym = s
                    break
                    
            if matched_sym:
                reply = QMessageBox.question(self.view, "Match Found", 
                    f"Found a symbol '{matched_sym.name}' that matches this orphaned file:\n{path.name}\n\nWould you like to automatically link this file to the symbol?", 
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    
                if reply == QMessageBox.StandardButton.Yes:
                    cat_clean = matched_sym.category[len(prefix):] if matched_sym.category.startswith(prefix) else matched_sym.category
                    
                    if i_type == 'orphan_fp':
                        dest_dir = lib_root / "Footprints" / f"{prefix}{cat_clean}.pretty"
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_path = dest_dir / path.name
                        if path.exists() and path.resolve() != dest_path.resolve():
                            shutil.move(str(path), str(dest_path))
                            path = dest_path
                        matched_sym.set_clean_property("Footprint", f"{prefix}{cat_clean}:{dest_path.stem}")
                        
                    elif i_type == 'orphan_ds':
                        dest_dir = lib_root / "Datasheets" / cat_clean
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_path = dest_dir / path.name
                        if path.exists() and path.resolve() != dest_path.resolve():
                            shutil.move(str(path), str(dest_path))
                            path = dest_path
                        matched_sym.set_clean_property("Datasheet", AssetManager.get_path_string(self.settings, lib_root, dest_path))
                        
                    elif i_type == 'orphan_mdl':
                        dest_dir = lib_root / "3D_Models" / cat_clean
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_path = dest_dir / path.name
                        if path.exists() and path.resolve() != dest_path.resolve():
                            shutil.move(str(path), str(dest_path))
                            path = dest_path
                            
                        matched_sym.set_clean_property("3D_Model", AssetManager.get_path_string(self.settings, lib_root, dest_path))
                        fp_str = matched_sym.get_clean_property("Footprint", "")
                        if fp_str and ":" in fp_str:
                            c, n = fp_str.split(":", 1)
                            fp_path = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                            if fp_path.exists():
                                KiCadIO.backup_file(fp_path, mb, bw, lib_root=lib_root)
                                with open(fp_path, 'r', encoding='utf-8') as f: fp_content = f.read()
                                mdl_path_str = AssetManager.get_path_string(self.settings, lib_root, dest_path)
                                if '(model "' in fp_content:
                                    fp_content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', fp_content)
                                else:
                                    last_paren = fp_content.rfind(')')
                                    if last_paren != -1:
                                        fp_content = fp_content[:last_paren] + f'\n  (model "{mdl_path_str}"\n    (offset (xyz 0 0 0))\n    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n' + fp_content[last_paren:]
                                with open(fp_path, 'w', encoding='utf-8') as f: f.write(fp_content)

                    elif i_type == 'orphan_img':
                        dest_dir = lib_root / "Images" / cat_clean
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_path = dest_dir / path.name
                        if path.exists() and path.resolve() != dest_path.resolve():
                            shutil.move(str(path), str(dest_path))
                            path = dest_path
                        try:
                            matched_sym.set_clean_property("Image_File", str(dest_path.relative_to(lib_root)).replace('\\', '/'))
                        except ValueError: pass
                                
                    cat_obj = self.app_library.categories.get(matched_sym.category)
                    if cat_obj: KiCadIO.write_category(cat_obj, mb, bw)
                    QMessageBox.information(self.view, "Success", f"Successfully linked {path.name} to {matched_sym.name}.")
                    continue
            
            reply = QMessageBox.question(self.view, "Resolve Orphan", f"'{path.name}' is an orphaned file not used by any symbol.\n\nDo you want to permanently Delete it from the drive?\n(Click No to ignore)", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    if path.is_file(): path.unlink()
                except OSError as e:
                    QMessageBox.warning(self.view, "Error", f"Failed to delete file: {e}")

        # --- 2. BROKEN LINK HANDLING (SMART AUTO-HEALER) ---
        broken_issues = [i for i in self.selected_issues if i.get('issue_type', '').startswith('broken_')]
        if broken_issues:
            healed = 0
            cleared = 0
            unhealed_issues = []
            cats_to_save = set()
            
            for issue in broken_issues:
                i_type = issue.get('issue_type', '')
                sym = issue.get('sym_obj')
                if not sym: continue
                
                cat_clean = sym.category[len(prefix):] if sym.category.startswith(prefix) else sym.category
                auto_healed = False
                
                # Attempt to auto-heal by locating the physical file in the new directories
                if i_type == 'broken_ds':
                    ds_name = Path(str(sym.get_clean_property("Datasheet", ""))).name
                    if ds_name:
                        attempt = lib_root / "Datasheets" / cat_clean / ds_name
                        if not attempt.exists(): attempt = lib_root / "Datasheets" / ds_name
                        if attempt.exists():
                            sym.set_clean_property("Datasheet", AssetManager.get_path_string(self.settings, lib_root, attempt))
                            auto_healed = True

                elif i_type in ['broken_mdl_prop', 'broken_fp_mdl']:
                    mdl_name = ""
                    if i_type == 'broken_mdl_prop':
                        mdl_name = Path(str(sym.get_clean_property("3D_Model", sym.get_clean_property("3D Model", "")))).name
                    else: # broken_fp_mdl
                        fp_path = issue.get('fp_path')
                        if fp_path and fp_path.exists():
                            try:
                                with open(fp_path, 'r', encoding='utf-8') as f: content = f.read()
                                m_match = re.search(r'\(model\s+"([^"]+)"', content)
                                if m_match: mdl_name = Path(m_match.group(1)).name
                            except Exception: pass
                    
                    if mdl_name:
                        attempt = lib_root / "3D_Models" / cat_clean / mdl_name
                        if not attempt.exists(): attempt = lib_root / "3D_Models" / mdl_name
                        if attempt.exists():
                            # Update base property
                            if i_type == 'broken_mdl_prop':
                                sym.set_clean_property("3D_Model", AssetManager.get_path_string(self.settings, lib_root, attempt))
                                
                            # Update footprint internal link
                            if i_type == 'broken_fp_mdl' or sym.get_clean_property("Footprint", ""):
                                fp_ref = sym.get_clean_property("Footprint", "")
                                if ":" in fp_ref:
                                    c, n = fp_ref.split(":", 1)
                                    fp_path = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                                    if fp_path.exists():
                                        KiCadIO.backup_file(fp_path, mb, bw, lib_root=lib_root)
                                        with open(fp_path, 'r', encoding='utf-8') as f: content = f.read()
                                        mdl_path_str = AssetManager.get_path_string(self.settings, lib_root, attempt)
                                        if '(model "' in content:
                                            content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', content)
                                        else:
                                            last_paren = content.rfind(')')
                                            if last_paren != -1:
                                                content = content[:last_paren] + f'\n  (model "{mdl_path_str}"\n    (offset (xyz 0 0 0))\n    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n' + content[last_paren:]
                                        with open(fp_path, 'w', encoding='utf-8') as f: f.write(content)
                            auto_healed = True
                            
                elif i_type == 'broken_img':
                    img_name = Path(str(sym.get_clean_property("Image_File", ""))).name
                    if img_name:
                        attempt = lib_root / "Images" / cat_clean / img_name
                        if not attempt.exists(): attempt = lib_root / "Images" / img_name
                        if attempt.exists():
                            try:
                                sym.set_clean_property("Image_File", str(attempt.relative_to(lib_root)).replace('\\', '/'))
                                auto_healed = True
                            except ValueError: pass
                
                elif i_type == 'broken_fp':
                    fp_ref = sym.get_clean_property("Footprint", "")
                    if ":" in fp_ref:
                        c, n = fp_ref.split(":", 1)
                        attempt = lib_root / "Footprints" / f"{prefix}{cat_clean}.pretty" / f"{n}.kicad_mod"
                        if attempt.exists():
                            sym.set_clean_property("Footprint", f"{prefix}{cat_clean}:{n}")
                            auto_healed = True

                if auto_healed:
                    healed += 1
                    cats_to_save.add(self.app_library.categories.get(sym.category))
                else:
                    unhealed_issues.append(issue)

            if unhealed_issues:
                reply = QMessageBox.question(self.view, "Clear Broken Links", f"Could not auto-heal {len(unhealed_issues)} broken links.\n\nDo you want to clear these missing references from the components?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply == QMessageBox.StandardButton.Yes:
                    for issue in unhealed_issues:
                        i_type = issue.get('issue_type')
                        sym = issue.get('sym_obj')
                        if i_type == 'broken_fp': sym.set_clean_property("Footprint", "")
                        elif i_type == 'broken_ds': sym.set_clean_property("Datasheet", "")
                        elif i_type == 'broken_img': sym.set_clean_property("Image_File", "")
                        elif i_type == 'broken_mdl_prop': 
                            sym.set_clean_property("3D_Model", "")
                            sym.set_clean_property("3D Model", "")
                        elif i_type == 'broken_fp_mdl':
                            fp_path = issue.get('fp_path')
                            if fp_path and fp_path.exists():
                                KiCadIO.backup_file(fp_path, mb, bw, lib_root=lib_root)
                                with open(fp_path, 'r', encoding='utf-8') as f: content = f.read()
                                content = re.sub(r'\(model\s+"[^"]+"', '(model ""', content)
                                with open(fp_path, 'w', encoding='utf-8') as f: f.write(content)
                        cleared += 1
                        cats_to_save.add(self.app_library.categories.get(sym.category))

            for cat in cats_to_save:
                if cat: KiCadIO.write_category(cat, mb, bw)
                
            if healed > 0 or cleared > 0:
                QMessageBox.information(self.view, "Broken Links Processed", f"Successfully Auto-Healed: {healed}\nManually Cleared: {cleared}")

        # --- 3. WRONG CATEGORY HANDLING ---
        wrong_cat_issues = [i for i in self.selected_issues if i.get('issue_type', '').startswith('wrong_cat_')]
        for issue in wrong_cat_issues:
            i_type = issue.get('issue_type', '')
            path = issue['path']
            expected = issue['expected_cat']
            reply = QMessageBox.question(self.view, "Wrong Category", f"'{path.name}' is in the wrong folder.\n\nMove it to the '{expected}' category?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    dest_dir = path.parent.parent / expected if i_type != 'wrong_cat_fp' else path.parent.parent / f"{prefix}{expected}.pretty"
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_path = dest_dir / path.name
                    shutil.move(str(path), str(dest_path))
                    
                    sym = issue.get('sym_obj')
                    if sym:
                        if i_type == 'wrong_cat_fp':
                            sym.set_clean_property("Footprint", f"{prefix}{expected}:{path.stem}")
                        elif i_type == 'wrong_cat_ds':
                            sym.set_clean_property("Datasheet", AssetManager.get_path_string(self.settings, lib_root, dest_path))
                        elif i_type == 'wrong_cat_img':
                            sym.set_clean_property("Image_File", str(dest_path.relative_to(lib_root)).replace('\\', '/'))
                            
                        cat = self.app_library.categories.get(sym.category)
                        if cat is not None: KiCadIO.write_category(cat, mb, bw)
                            
                        if i_type == 'wrong_cat_mdl':
                            fp_path = issue.get('fp_path')
                            if fp_path and fp_path.exists():
                                KiCadIO.backup_file(fp_path, mb, bw, lib_root=lib_root)
                                with open(fp_path, 'r', encoding='utf-8') as f: content = f.read()
                                content = content.replace(str(path).replace('\\', '/'), str(dest_path).replace('\\', '/'))
                                with open(fp_path, 'w', encoding='utf-8') as f: f.write(content)
                except Exception as e:
                    QMessageBox.warning(self.view, "Error", f"Failed to move file: {e}")

        self.reload_library(preserve_state=True)

    def open_selected_location(self):
        if len(self.selected_issues) != 1: return
        issue = self.selected_issues[0]
        
        target_path = issue.get('path') or issue.get('file')
        if target_path and isinstance(target_path, Path) and target_path.exists():
            try:
                if platform.system() == "Windows":
                    import subprocess
                    subprocess.run(['explorer', '/select,', os.path.normpath(str(target_path))])
                elif platform.system() == "Darwin":
                    import subprocess
                    subprocess.run(['open', '-R', str(target_path)])
                else:
                    from PySide6.QtGui import QDesktopServices
                    from PySide6.QtCore import QUrl
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_path.parent)))
            except Exception as e:
                logger.error(f"Failed to open location: {e}")
        else:
            QMessageBox.warning(self.view, "Not Found", "The file location could not be found or the file no longer exists.")

    def delete_selected_parts(self):
        mb = get_setting_int(self.settings, "backups_to_keep", 5)
        bw = get_setting_int(self.settings, "backup_window_minutes", 5)
        
        # Handle standalone file deletion (like orphaned physical files)
        if not self.selected_symbols and self.selected_issues:
            paths_to_delete = [issue['path'] for issue in self.selected_issues if issue.get('issue_type', '').startswith('orphan_') and 'path' in issue]
            
            if paths_to_delete:
                names = [p.name for p in paths_to_delete]
                reply = QMessageBox.question(self.view, "Confirm Delete", 
                    f"Are you sure you want to permanently delete {len(names)} orphaned file(s)?\n\n" + 
                    "\n".join(names[:5]) + ("\n..." if len(names) > 5 else ""), 
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    
                if reply == QMessageBox.StandardButton.Yes:
                    for p in paths_to_delete:
                        try:
                            if p.is_file(): p.unlink()
                        except OSError: pass
                    self.reload_library(preserve_state=True)
                    QMessageBox.information(self.view, "Success", "Orphaned files deleted successfully!")
            return
            
        if not self.selected_symbols: return
        
        names = [s.name for s in self.selected_symbols]
        reply = QMessageBox.question(self.view, "Confirm Delete", 
            f"Are you sure you want to permanently delete {len(names)} part(s)?\n\n" + 
            "\n".join(names[:5]) + ("\n..." if len(names) > 5 else ""), 
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
        if reply == QMessageBox.StandardButton.Yes:
            categories_to_save = set()
            assets_to_check = set()
            
            for sym in self.selected_symbols:
                fp = sym.get_clean_property("Footprint", "")
                ds = sym.get_clean_property("Datasheet", "")
                if fp: assets_to_check.add(f"fp:{fp}")
                if ds: assets_to_check.add(f"ds:{ds}")
            
            for sym in self.selected_symbols:
                cat = self.app_library.categories.get(sym.category)
                if cat is not None:
                    cat.remove_symbol_by_name(sym.name)
                    categories_to_save.add(cat)
                    
            for cat in categories_to_save:
                if cat is not None:
                    KiCadIO.write_category(cat, mb, bw)
                    if not cat.symbols and cat.filepath and cat.filepath.exists():
                        try: cat.filepath.unlink()
                        except OSError: pass
                    
            in_use = set()
            for cat in self.app_library.categories.values():
                for sym in cat.symbols:
                    fp = sym.get_clean_property("Footprint", "")
                    ds = sym.get_clean_property("Datasheet", "")
                    if fp: in_use.add(f"fp:{fp}")
                    if ds: in_use.add(f"ds:{ds}")
            
            orphaned = assets_to_check - in_use
            lib_root = self.app_library.root_path
            path_var = get_setting_str(self.settings, "kicad_path_var", "").strip()
            
            if lib_root:
                for asset in orphaned:
                    if asset.startswith("ds:"):
                        ds_str = asset[3:]
                        if path_var: ds_str = ds_str.replace(f"{path_var}/", "")
                        ds_path = Path(ds_str)
                        if not ds_path.is_absolute(): ds_path = lib_root / ds_path
                        if ds_path.exists() and lib_root in ds_path.parents:
                            try: ds_path.unlink()
                            except OSError: pass
                    elif asset.startswith("fp:"):
                        fp_str = asset[3:]
                        if ":" in fp_str:
                            c, n = fp_str.split(":", 1)
                            fp_path = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                            if fp_path.exists():
                                try:
                                    with open(fp_path, 'r', encoding='utf-8') as f:
                                        content = f.read()
                                    m_match = re.search(r'\(model\s+"([^"]+)"', content)
                                    if m_match:
                                        m_path = Path(m_match.group(1))
                                        if not m_path.is_absolute():
                                            clean_m_str = str(m_path).replace("${KIPRJMOD}/", "").replace("${KICAD6_3DMODEL_DIR}/", "")
                                            m_path = lib_root / Path(clean_m_str)
                                        if m_path.exists() and lib_root in m_path.parents:
                                            m_path.unlink()
                                except Exception: pass
                                
                                try: fp_path.unlink()
                                except OSError: pass
                    
            self.reload_library(preserve_state=True)
            QMessageBox.information(self.view, "Success", "Parts and orphaned files deleted successfully!")

    def _apply_symbol_update(self, old_sym: Symbol, new_name: str, new_cat_name: str, new_props: dict):
        mb = get_setting_int(self.settings, "backups_to_keep", 5)
        bw = get_setting_int(self.settings, "backup_window_minutes", 5)
        
        old_cat = self.app_library.categories.get(old_sym.category)
        if old_cat is not None:
            old_cat.remove_symbol_by_name(old_sym.name)
        
        updated_sym = Symbol(name=new_name, category=new_cat_name)
        updated_sym.properties = new_props
        updated_sym.raw_graphics_block = old_sym.raw_graphics_block
        
        if old_sym.name != new_name:
            updated_sym.raw_graphics_block = KiCadIO.rename_symbol_in_block(updated_sym.raw_graphics_block, old_sym.name, new_name)
            
        new_cat = self.app_library.categories.get(new_cat_name)
        if not new_cat:
            sym_dir = self.app_library.get_symbols_dir()
            if sym_dir is None: return None
            filepath = sym_dir / f"{new_cat_name}.kicad_sym"
            new_cat = Category(new_cat_name, filepath)
            self.app_library.categories[new_cat_name] = new_cat
            
        if new_cat is not None:
            new_cat.add_symbol(updated_sym)
            KiCadIO.write_category(new_cat, mb, bw)
            
        if old_cat is not None and new_cat is not None and old_cat.name != new_cat.name:
            KiCadIO.write_category(old_cat, mb, bw)
            if not old_cat.symbols and old_cat.filepath and old_cat.filepath.exists():
                try: old_cat.filepath.unlink()
                except OSError: pass
                
        return updated_sym

    def edit_selected_part(self):
        mb = get_setting_int(self.settings, "backups_to_keep", 5)
        bw = get_setting_int(self.settings, "backup_window_minutes", 5)
        path_var_raw = get_setting_str(self.settings, "kicad_path_var", "").strip()
        
        if len(self.selected_symbols) != 1: return
        target_sym = self.selected_symbols[0]
        
        while target_sym:
            prefix = get_library_prefix(self.settings)
            cat_name = target_sym.category
            if cat_name.startswith(prefix): cat_name = cat_name[len(prefix):]
            
            prefilled_assets = {}
            fp_str = target_sym.get_clean_property("Footprint", "")
            if fp_str and ":" in fp_str:
                c, n = fp_str.split(":", 1)
                lib_root = self.app_library.root_path
                if lib_root:
                    fp_path = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                    if fp_path.exists():
                        prefilled_assets['footprint'] = str(fp_path)
                        try:
                            with open(fp_path, 'r', encoding='utf-8') as f:
                                fp_content = f.read()
                            m_match = re.search(r'\(model\s+"([^"]+)"', fp_content)
                            if m_match:
                                m_path_str = m_match.group(1)
                                if m_path_str:
                                    clean_m_str = m_path_str.replace("${KIPRJMOD}/", "").replace("${KICAD6_3DMODEL_DIR}/", "")
                                    if path_var_raw: clean_m_str = clean_m_str.replace(f"{path_var_raw}/", "")
                                    m_path = Path(clean_m_str)
                                    if not m_path.is_absolute():
                                        m_path = lib_root / m_path
                                    
                                    if not m_path.exists():
                                        local_attempt = lib_root / "3D_Models" / c / m_path.name
                                        if local_attempt.exists():
                                            m_path = local_attempt
                                    if m_path.exists():
                                        prefilled_assets['model'] = str(m_path)
                        except Exception: pass
            
            dialog = SymbolEditorDialog(self.view, symbol=target_sym, tmp_dir=self.tmp_folder, prefilled_assets=prefilled_assets, cached_names=self.cached_part_names, is_batch=False, is_edit=True)
            
            # Determine prev/next BEFORE the dialog applies any saves
            cat_obj = self.app_library.categories.get(target_sym.category)
            prev_sym_name = None
            next_sym_name = None
            target_cat_name = target_sym.category
            
            if cat_obj:
                sorted_syms = sorted(cat_obj.symbols, key=lambda s: s.name.lower())
                try:
                    idx = sorted_syms.index(target_sym)
                    if idx > 0: prev_sym_name = sorted_syms[idx - 1].name
                    if idx < len(sorted_syms) - 1: next_sym_name = sorted_syms[idx + 1].name
                except ValueError:
                    pass
            
            dialog.btn_prev.setEnabled(bool(prev_sym_name))
            dialog.btn_next.setEnabled(bool(next_sym_name))
            
            if prefilled_assets.get('footprint'): 
                dialog.footprint_drop.is_library_link = True
                
            subcat_val = target_sym.get_clean_property("Subcategory", "")
            combo = dialog.subcat_combo
            if subcat_val and combo is not None:
                combo.blockSignals(True)
                if combo.findText(subcat_val) == -1:
                    ins_idx = max(0, combo.count() - 2)
                    combo.insertItem(ins_idx, subcat_val)
                combo.setCurrentText(subcat_val)
                combo.blockSignals(False)
                
            def attempt_save(d, current_target=target_sym):
                new_props = d.get_updated_properties()
                new_ui_cat = d.category_combo.currentText()
                new_name = d.get_part_name()
                
                target_cat_full = f"{prefix}{new_ui_cat}"
                lib_root = self.app_library.root_path
                
                if lib_root is None: return False
                
                try:
                    # 1. Rename existing assets if part name or category changed
                    AssetManager.smart_rename_assets(current_target.name, new_name, current_target, target_cat_full, new_props, lib_root)

                    # 2. Process NEW dropped assets
                    fp_dir = lib_root / "Footprints" / f"{target_cat_full}.pretty"
                    model_dir = lib_root / "3D_Models" / new_ui_cat
                    doc_dir = lib_root / "Datasheets" / new_ui_cat
                    img_dir = lib_root / "Images" / new_ui_cat

                    # Footprint
                    if d.footprint_drop.file_path and not getattr(d.footprint_drop, 'is_library_link', False):
                        fp_dir.mkdir(parents=True, exist_ok=True)
                        src_fp = Path(d.footprint_drop.file_path)
                        dst_fp = fp_dir / f"{new_name}.kicad_mod"
                        if src_fp.exists() and src_fp.resolve() != dst_fp.resolve():
                            shutil.copy2(src_fp, dst_fp)
                        new_props["Footprint"] = f"{target_cat_full}:{new_name}"
                    elif not d.footprint_drop.file_path and getattr(d.footprint_drop, "cleared_by_user", False):
                        new_props.pop("Footprint", None)

                    # Model
                    if d.model_drop.file_path:
                        src_mdl = Path(d.model_drop.file_path)
                        if not (lib_root in src_mdl.parents): # New external file
                            model_dir.mkdir(parents=True, exist_ok=True)
                            dst_mdl = model_dir / f"{new_name}{src_mdl.suffix}"
                            if src_mdl.exists() and src_mdl.resolve() != dst_mdl.resolve():
                                shutil.copy2(src_mdl, dst_mdl)
                            
                            new_props["3D_Model"] = AssetManager.get_path_string(self.settings, lib_root, dst_mdl)

                            # Inject into footprint
                            fp_ref = new_props.get("Footprint", "")
                            if fp_ref and ":" in fp_ref:
                                c, n = fp_ref.split(":", 1)
                                fp_path = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                                if fp_path.exists():
                                    KiCadIO.backup_file(fp_path, mb, bw, lib_root=lib_root)
                                    with open(fp_path, 'r', encoding='utf-8') as f: fp_content = f.read()
                                    mdl_path_str = AssetManager.get_path_string(self.settings, lib_root, dst_mdl)
                                    if '(model "' in fp_content:
                                        fp_content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', fp_content)
                                    else:
                                        last_paren = fp_content.rfind(')')
                                        if last_paren != -1:
                                            fp_content = fp_content[:last_paren] + f'\n  (model "{mdl_path_str}"\n    (offset (xyz 0 0 0))\n    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n' + fp_content[last_paren:]
                                    with open(fp_path, 'w', encoding='utf-8') as f: f.write(fp_content)
                    elif getattr(d.model_drop, "cleared_by_user", False):
                        new_props.pop("3D_Model", None)
                        new_props.pop("3D Model", None)
                        fp_ref = new_props.get("Footprint", "")
                        if fp_ref and ":" in fp_ref:
                            c, n = fp_ref.split(":", 1)
                            fp_path = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                            if fp_path.exists():
                                KiCadIO.backup_file(fp_path, mb, bw, lib_root=lib_root)
                                with open(fp_path, 'r', encoding='utf-8') as f: fp_content = f.read()
                                fp_content = re.sub(r'\(model\s+"[^"]+"', '(model ""', fp_content)
                                with open(fp_path, 'w', encoding='utf-8') as f: f.write(fp_content)

                    # Datasheet
                    if d.datasheet_drop.file_path:
                        src_ds = Path(d.datasheet_drop.file_path)
                        if not (lib_root in src_ds.parents):
                            doc_dir.mkdir(parents=True, exist_ok=True)
                            dst_ds = doc_dir / f"{new_name}{src_ds.suffix}"
                            if src_ds.exists() and src_ds.resolve() != dst_ds.resolve():
                                shutil.copy2(src_ds, dst_ds)
                            new_props["Datasheet"] = AssetManager.get_path_string(self.settings, lib_root, dst_ds)
                    elif getattr(d.datasheet_drop, "cleared_by_user", False):
                        new_props.pop("Datasheet", None)

                    # Image
                    if d.img_lbl.file_path:
                        src_img = Path(d.img_lbl.file_path)
                        if not (lib_root in src_img.parents):
                            img_dir.mkdir(parents=True, exist_ok=True)
                            dst_img = img_dir / f"{new_name}{src_img.suffix}"
                            if src_img.exists() and src_img.resolve() != dst_img.resolve():
                                shutil.copy2(src_img, dst_img)
                            try:
                                new_props["Image_File"] = str(dst_img.relative_to(lib_root)).replace('\\', '/')
                            except ValueError: pass
                    elif not d.img_lbl.file_path:
                        new_props.pop("Image_File", None)

                    updated_sym = self._apply_symbol_update(current_target, new_name, target_cat_full, new_props)
                    if updated_sym:
                        d.symbol = updated_sym
                    self.reload_library(preserve_state=True)
                    return True
                except Exception as e:
                    logger.exception("Save failed")
                    QMessageBox.critical(self.view, "Error", f"Save failed: {e}")
                    return False

            dialog.save_callback = attempt_save
            dialog.exec()
            
            nav = getattr(dialog, 'nav_intent', None)
            if not nav:
                break
                
            # If navigating, look up the next/prev symbol in the newly updated library structure!
            target_sym = None
            new_cat_obj = self.app_library.categories.get(target_cat_name)
            if new_cat_obj:
                if nav == 'prev' and prev_sym_name:
                    target_sym = new_cat_obj.get_symbol(prev_sym_name)
                elif nav == 'next' and next_sym_name:
                    target_sym = new_cat_obj.get_symbol(next_sym_name)

    def _can_silent_merge(self, sym1: Symbol, sym2: Symbol) -> tuple[bool, dict]:
        if sym1.name != sym2.name: return False, {}
        
        p1 = {p.number: p for p in sym1.pins}
        p2 = {p.number: p for p in sym2.pins}
        if set(p1.keys()) != set(p2.keys()): return False, {}
        for n, p in p1.items():
            if p.name != p2[n].name or p.direction != p2[n].direction or p.at != p2[n].at:
                return False, {}
                
        merged_props = {}
        all_keys = set(sym1.properties.keys()).union(set(sym2.properties.keys()))
        for k in all_keys:
            if k.lower() in ["uuid", "suggested_category"]: continue
            v1 = str(sym1.properties.get(k, "")).strip()
            v2 = str(sym2.properties.get(k, "")).strip()
            
            if v1 == v2: merged_props[k] = v1
            elif not v1: merged_props[k] = v2
            elif not v2: merged_props[k] = v1
            elif v1.lower() in v2.lower(): merged_props[k] = v2
            elif v2.lower() in v1.lower(): merged_props[k] = v1
            else: return False, {} 
            
        return True, merged_props

    def merge_selected_parts(self):
        mb = get_setting_int(self.settings, "backups_to_keep", 5)
        bw = get_setting_int(self.settings, "backup_window_minutes", 5)
        
        if len(self.selected_symbols) < 2: return
        
        parts = self.selected_symbols
        prefix = get_library_prefix(self.settings)
        
        current_sym = parts[0]
        
        for next_sym in parts[1:]:
            dialog = MergeComparisonDialog(self.view, left_sym=current_sym, right_sym=next_sym, reasons=["Manual Merge Selection"])
            res = dialog.exec()
            
            if res != int(QDialog.DialogCode.Accepted):
                logger.info("Merge operation cancelled by user mid-sequence.")
                self.reload_library(preserve_state=True)
                return
                
            merged_data = dialog.get_merged_data()
            
            try:
                cat_current = self.app_library.categories.get(current_sym.category)
                if cat_current is not None: cat_current.remove_symbol_by_name(current_sym.name)
                
                cat_next = self.app_library.categories.get(next_sym.category)
                if cat_next is not None: cat_next.remove_symbol_by_name(next_sym.name)
                
                base_block = current_sym.raw_graphics_block if merged_data['base_symbol'] == 'left' else next_sym.raw_graphics_block
                target_cat_full = f"{prefix}{merged_data['category']}"
                
                final_sym = Symbol(merged_data['name'], target_cat_full)
                final_sym.properties = merged_data['props']
                final_sym.raw_graphics_block = base_block
                
                if final_sym.name != current_sym.name and final_sym.name != next_sym.name:
                    final_sym.raw_graphics_block = KiCadIO.rename_symbol_in_block(final_sym.raw_graphics_block, current_sym.name, final_sym.name)

                new_cat = self.app_library.categories.get(target_cat_full)
                if not new_cat:
                    sym_dir = self.app_library.get_symbols_dir()
                    if sym_dir is None: 
                        self.reload_library(preserve_state=True)
                        return
                        
                    filepath = sym_dir / f"{target_cat_full}.kicad_sym"
                    new_cat = Category(target_cat_full, filepath)
                    self.app_library.categories[target_cat_full] = new_cat
                    
                if new_cat is not None:
                    new_cat.add_symbol(final_sym)
                    KiCadIO.write_category(new_cat, mb, bw)
                
                if cat_current is not None and new_cat is not None and cat_current.name != new_cat.name: 
                    KiCadIO.write_category(cat_current, mb, bw)
                    if not cat_current.symbols and cat_current.filepath and cat_current.filepath.exists():
                        try: cat_current.filepath.unlink()
                        except OSError: pass
                        
                if cat_next is not None and new_cat is not None and cat_current is not None and cat_next.name != new_cat.name and cat_next.name != cat_current.name: 
                    KiCadIO.write_category(cat_next, mb, bw)
                    if not cat_next.symbols and cat_next.filepath and cat_next.filepath.exists():
                        try: cat_next.filepath.unlink()
                        except OSError: pass
                
                current_sym = final_sym
                
            except Exception as e:
                logger.exception("Merge failed")
                QMessageBox.critical(self.view, "Merge Error", f"Failed to merge:\n{e}")
                self.reload_library(preserve_state=True)
                return

        self.reload_library(preserve_state=True)
        QMessageBox.information(self.view, "Success", "Selected parts were successfully merged!")

    # --- File Input / Output & API Workflows ---

    def process_dropped_files(self, dropped_files):
        if not self.app_library.is_valid():
            QMessageBox.warning(self.view, "Setup Required", "Please configure your library root first.")
            self.view.open_settings()
            if not self.app_library.is_valid(): return

        # Defer the actual processing so the OS drag-and-drop event can finish and unfreeze Windows Explorer!
        QTimer.singleShot(10, lambda: self._deferred_process_dropped_files(dropped_files))

    def _deferred_process_dropped_files(self, dropped_files):
        files_dropped = []
        for f in dropped_files:
            p = Path(f)
            if p.is_dir():
                for root, _, files in os.walk(p):
                    for file in files:
                        files_dropped.append(str(Path(root) / file))
            elif p.is_file():
                files_dropped.append(str(p))

        if not files_dropped: return

        self.view.set_working(True, f"Processing {len(files_dropped)} dropped item(s)...")
        import_batches = []
        bundled_loose = []
        
        for f in files_dropped:
            p = Path(f)
            if p.suffix.lower() in ['.zip', '.elibz']: import_batches.append([f])
            elif p.is_file(): bundled_loose.append(f)
                
        if bundled_loose: import_batches.append(bundled_loose)

        successes, skips, failures = [], [], []
        is_batch = len(import_batches) > 1
        
        mb = get_setting_int(self.settings, "backups_to_keep", 5)
        bw = get_setting_int(self.settings, "backup_window_minutes", 5)

        for batch_files in import_batches:
            if self.tmp_folder.exists(): shutil.rmtree(self.tmp_folder)
            self.tmp_folder.mkdir()

            try:
                self.view.set_working(True, "Extracting and scanning assets...")
                assets = FileImporter.extract_and_scan(batch_files, self.tmp_folder)
            except Exception as e:
                failures.append(("Asset Extraction", str(e)))
                continue

            if not assets['symbol'] and assets['footprint']:
                for fp_file in assets['footprint']:
                    self.view.set_working(False, "Awaiting user input...")
                    status, part_name, err = self.run_footprint_only_flow(assets, fp_file)
                    self.view.set_working(True, "Finalizing...")
                    
                    if status == "SUCCESS": successes.append(part_name)
                    elif status == "SKIPPED": skips.append(part_name)
                    elif status == "ERROR": failures.append((part_name, err))
                continue

            if not assets['symbol']:
                failures.append(("Unknown Asset", "No valid KiCad symbols or footprints found in batch."))
                continue

            parsed_cats = []
            total_syms_in_batch = 0
            for sym_file in assets['symbol']:
                downloaded_cat = KiCadIO.parse_category(sym_file)
                if not downloaded_cat or not downloaded_cat.symbols:
                    failures.append((Path(sym_file).name, "Could not parse any symbols from this file."))
                    continue
                parsed_cats.append(downloaded_cat)
                total_syms_in_batch += len(downloaded_cat.symbols)

            batch_aborted = False
            
            for downloaded_cat in parsed_cats:
                for source_sym in downloaded_cat.symbols:
                    original_name = source_sym.name
                    
                    if self.dk_api.is_configured():
                        terms_to_try = []
                        mpn = source_sym.get_clean_property("MPN")
                        if mpn and mpn != "-": terms_to_try.append(mpn)
                        dk_pn = source_sym.get_clean_property("DigiKey_PN") or source_sym.get_clean_property("Supplier Part")
                        if dk_pn and dk_pn != "-": terms_to_try.append(dk_pn)
                        if original_name: terms_to_try.append(original_name)
                        
                        terms_to_try = list(dict.fromkeys(terms_to_try))
                        
                        dk_data = None
                        for term in terms_to_try:
                            self.view.set_working(True, f"Auto-Querying Digi-Key for '{term}'...")
                            dk_data = self.dk_api.search_part(term)
                            if dk_data: break
                            self.view.set_working(True, f"'{term}' not found. Trying next...")
                            QThread.msleep(800)
                            
                        if dk_data:
                            for k, v in dk_data.items():
                                if v and not source_sym.properties.get(k): 
                                    source_sym.set_clean_property(k, v)
                            
                            if dk_data.get("Datasheet") and not assets.get('datasheet'):
                                dest_pdf = self.tmp_folder / f"{original_name}_Datasheet.pdf"
                                if self.dk_api.download_datasheet(dk_data["Datasheet"], dest_pdf):
                                    assets['datasheet'].append(dest_pdf)
                                    
                            if dk_data.get("Image_URL") and not source_sym.properties.get('Image_File'):
                                img_url = dk_data["Image_URL"]
                                img_ext = ".jpg" if ".jpg" in img_url.lower() else ".png"
                                dest_img = self.tmp_folder / f"{original_name}_Image{img_ext}"
                                try:
                                    req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                                    img_data = urllib.request.urlopen(req, timeout=10).read()
                                    with open(dest_img, 'wb') as f: f.write(img_data)
                                    source_sym.set_clean_property("Image_File", str(dest_img))
                                except Exception as e:
                                    logger.error(f"Failed to auto-download image: {e}")

                    self.view.set_working(False, "Awaiting user input...")
                    
                    def match_asset(lst, sym_name):
                        if not lst: return None
                        for asset in lst:
                            if Path(asset).stem.lower() == sym_name.lower(): return asset
                        if len(lst) == 1 and total_syms_in_batch == 1: return lst[0]
                        for asset in lst:
                            astem = Path(asset).stem.lower()
                            if astem and (astem in sym_name.lower() or sym_name.lower() in astem): return asset
                        return None
                        
                    def match_alternate_assets(lst, sym_name, primary_asset):
                        alts = []
                        if not lst: return alts
                        for asset in lst:
                            if asset == primary_asset: continue
                            astem = Path(asset).stem.lower()
                            if astem.startswith(f"{sym_name.lower()}_") or astem.startswith(f"{sym_name.lower()}-"):
                                alts.append(asset)
                        return alts

                    primary_fp = match_asset(assets['footprint'], original_name)
                    primary_mdl = match_asset(assets['model'], original_name)
                    primary_ds = match_asset(assets['datasheet'], original_name)
                        
                    single_asset = {
                        'footprint': primary_fp,
                        'model': primary_mdl,
                        'datasheet': primary_ds
                    }
                    
                    alternate_assets = {
                        'footprint': match_alternate_assets(assets['footprint'], original_name, primary_fp),
                        'model': match_alternate_assets(assets['model'], original_name, primary_mdl),
                        'datasheet': match_alternate_assets(assets['datasheet'], original_name, primary_ds)
                    }
                    
                    status, part_name, err = self.run_import_flow(source_sym, original_name, single_asset, is_batch, alternate_assets)
                    self.view.set_working(True, "Finalizing import...")
                    
                    if status == "SUCCESS": successes.append(part_name)
                    elif status == "SKIPPED": skips.append(part_name)
                    elif status == "ABORT":
                        skips.append(part_name)
                        batch_aborted = True
                        break
                    elif status == "ERROR": failures.append((part_name, err))

            if batch_aborted: break
            
        self.cleanup_originals(files_dropped)

        if self.tmp_folder.exists(): shutil.rmtree(self.tmp_folder)
        self.view.set_working(False, "Ready")
        self.reload_library(preserve_state=True)
        self.view.show_digest("Drag & Drop Import", successes, skips, failures)

    def run_footprint_only_flow(self, assets, fp_path: Path):
        dialog = LinkFilesDialog(self.view, fp_path.stem, self.cached_part_names)
        if dialog.exec() == int(QDialog.DialogCode.Accepted):
            target_name = dialog.get_selected_symbol()
            if not target_name: return "SKIPPED", fp_path.stem, "No symbol selected."
            
            target_sym = None
            for cat in self.app_library.categories.values():
                target_sym = cat.get_symbol(target_name)
                if target_sym: break
                
            if not target_sym: return "ERROR", fp_path.stem, "Symbol not found in memory."
            
            lib_root = self.app_library.root_path
            if lib_root is None: return "ERROR", fp_path.stem, "Library root not set."
            
            prefix = get_library_prefix(self.settings)
            cat_clean = target_sym.category[len(prefix):] if target_sym.category.startswith(prefix) else target_sym.category
            fp_name = FileImporter.sanitize_name(fp_path.stem) if get_setting_bool(self.settings, "sanitize_names", True) else fp_path.stem
            
            try:
                fp_dir = lib_root / "Footprints" / f"{prefix}{cat_clean}.pretty"
                model_dir = lib_root / "3D_Models" / cat_clean
                fp_dir.mkdir(parents=True, exist_ok=True)
                
                dst_fp = fp_dir / f"{fp_name}.kicad_mod"
                shutil.copy2(fp_path, dst_fp)
                
                if assets.get('model'):
                    src_mdl = Path(assets['model'][0])
                    model_dir.mkdir(parents=True, exist_ok=True)
                    dst_mdl = model_dir / f"{fp_name}{src_mdl.suffix}"
                    if src_mdl.exists() and src_mdl.resolve() != dst_mdl.resolve():
                        shutil.copy2(src_mdl, dst_mdl)
                    
                    mdl_path_str = AssetManager.get_path_string(self.settings, lib_root, dst_mdl)
                    with open(dst_fp, 'r', encoding='utf-8') as f: fp_content = f.read()
                    if '(model "' in fp_content:
                        fp_content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', fp_content)
                    else:
                        last_paren = fp_content.rfind(')')
                        fp_content = fp_content[:last_paren] + f'\n  (model "{mdl_path_str}"\n    (offset (xyz 0 0 0))\n    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n' + fp_content[last_paren:]
                    with open(dst_fp, 'w', encoding='utf-8') as f: f.write(fp_content)
                    
                    target_sym.set_clean_property("3D_Model", AssetManager.get_path_string(self.settings, lib_root, dst_mdl))
                    
                if dialog.set_default:
                    target_sym.set_clean_property("Footprint", f"{prefix}{cat_clean}:{fp_name}")
                    
                if assets.get('datasheet'):
                    doc_dir = lib_root / "Datasheets" / cat_clean
                    doc_dir.mkdir(parents=True, exist_ok=True)
                    src_ds = Path(assets['datasheet'][0])
                    dst_ds = doc_dir / f"{fp_name}{src_ds.suffix}"
                    if src_ds.exists() and src_ds.resolve() != dst_ds.resolve():
                        shutil.copy2(src_ds, dst_ds)
                    target_sym.set_clean_property("Datasheet", AssetManager.get_path_string(self.settings, lib_root, dst_ds))
                
                mb = get_setting_int(self.settings, "backups_to_keep", 5)
                bw = get_setting_int(self.settings, "backup_window_minutes", 5)
                cat_obj = self.app_library.categories.get(target_sym.category)
                if cat_obj: KiCadIO.write_category(cat_obj, mb, bw)
                
                return "SUCCESS", target_name, ""
            except Exception as e:
                return "ERROR", fp_path.stem, str(e)
                
        return "SKIPPED", fp_path.stem, "User cancelled."

    def run_import_flow(self, source_sym: Symbol, original_name: str, ui_assets: dict, is_batch: bool, alternate_assets: Optional[Dict] = None):
        if alternate_assets is None: alternate_assets = {}
        
        lib_root = self.app_library.root_path
        if lib_root is None:
            return "ERROR", source_sym.name, "Library root not configured."
            
        dialog = SymbolEditorDialog(self.view, symbol=source_sym, tmp_dir=self.tmp_folder, prefilled_assets=ui_assets, 
                                    cached_names=self.cached_part_names, is_batch=is_batch, is_edit=False)

        while True:
            result = dialog.exec()
            if result == int(QDialog.DialogCode.Accepted):
                final_name = dialog.get_part_name()
                target_cat_raw = dialog.get_category()
                prefix = get_library_prefix(self.settings)
                target_cat_full = f"{prefix}{target_cat_raw}"
                
                dup_sym = None
                mpn_widget = dialog.inputs.get("MPN")
                new_mpn = mpn_widget.text().strip().lower() if isinstance(mpn_widget, QLineEdit) else ""
                dk_widget = dialog.inputs.get("DigiKey_PN")
                new_dk = dk_widget.text().strip().lower() if isinstance(dk_widget, QLineEdit) else ""
                
                for s in self.app_library.get_all_symbols():
                    if s.name == final_name: dup_sym = s; break
                    if new_mpn and new_mpn != '-' and s.get_clean_property("MPN").lower() == new_mpn: dup_sym = s; break
                    if new_dk and new_dk != '-' and s.get_clean_property("DigiKey_PN", s.get_clean_property("Supplier Part")).lower() == new_dk: dup_sym = s; break

                source_sym.name = final_name
                source_sym.category = target_cat_full
                source_sym.properties = dialog.get_updated_properties()
                
                if dup_sym:
                    can_silent, silent_props = self._can_silent_merge(source_sym, dup_sym)
                    if can_silent:
                        logger.info(f"Auto-merging additional data for {source_sym.name} without UI.")
                        source_sym.properties = silent_props
                        source_sym.raw_graphics_block = dup_sym.raw_graphics_block
                        old_cat = self.app_library.categories.get(dup_sym.category)
                        if old_cat: old_cat.remove_symbol_by_name(dup_sym.name)
                    else:
                        merge_dialog = MergeComparisonDialog(self.view, left_sym=source_sym, right_sym=dup_sym, reasons=["Duplicate Detected on Import"])
                        m_result = merge_dialog.exec()
                        
                        if m_result == int(QDialog.DialogCode.Accepted):
                            action = merge_dialog.final_action
                            if action == 'merge':
                                merged_data = merge_dialog.get_merged_data()
                                source_sym.name = merged_data['name']
                                source_sym.category = f"{prefix}{merged_data['category']}"
                                source_sym.properties = merged_data['props']
                                if merged_data['base_symbol'] == 'right':
                                    source_sym.raw_graphics_block = dup_sym.raw_graphics_block
                                    
                                old_cat = self.app_library.categories.get(dup_sym.category)
                                if old_cat: old_cat.remove_symbol_by_name(dup_sym.name)
                            elif action == 'add_new': pass 
                        else: continue 

                try:
                    fp_dir = lib_root / "Footprints" / f"{target_cat_full}.pretty"
                    model_dir = lib_root / "3D_Models" / target_cat_raw
                    doc_dir = lib_root / "Datasheets" / target_cat_raw
                    img_dir = lib_root / "Images" / target_cat_raw
                    for d in [fp_dir, model_dir, doc_dir, img_dir]: d.mkdir(parents=True, exist_ok=True)
                    
                    # 1. Handle Alternate Assets silently
                    if alternate_assets:
                        for alt_fp_str in alternate_assets.get('footprint', []):
                            alt_fp = Path(alt_fp_str)
                            if alt_fp.exists():
                                suffix = alt_fp.stem[len(original_name):] if alt_fp.stem.lower().startswith(original_name.lower()) else f"_{alt_fp.stem}"
                                dst_alt_fp = fp_dir / f"{final_name}{suffix}.kicad_mod"
                                shutil.copy2(alt_fp, dst_alt_fp)
                                
                                alt_mdl_match = None
                                for alt_mdl_str in alternate_assets.get('model', []):
                                    if Path(alt_mdl_str).stem.lower() == alt_fp.stem.lower():
                                        alt_mdl_match = Path(alt_mdl_str)
                                        break
                                        
                                if alt_mdl_match and alt_mdl_match.exists():
                                    dst_alt_mdl = model_dir / f"{final_name}{suffix}{alt_mdl_match.suffix}"
                                    shutil.copy2(alt_mdl_match, dst_alt_mdl)
                                    with open(dst_alt_fp, 'r', encoding='utf-8') as f: fp_content = f.read()
                                    mdl_path_str = AssetManager.get_path_string(self.settings, lib_root, dst_alt_mdl)
                                    if '(model "' in fp_content:
                                        fp_content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', fp_content)
                                    else:
                                        last_paren = fp_content.rfind(')')
                                        if last_paren != -1:
                                            fp_content = fp_content[:last_paren] + f'\n  (model "{mdl_path_str}"\n    (offset (xyz 0 0 0))\n    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n' + fp_content[last_paren:]
                                    with open(dst_alt_fp, 'w', encoding='utf-8') as f: f.write(fp_content)

                        for alt_ds_str in alternate_assets.get('datasheet', []):
                            alt_ds = Path(alt_ds_str)
                            if alt_ds.exists():
                                suffix = alt_ds.stem[len(original_name):] if alt_ds.stem.lower().startswith(original_name.lower()) else f"_{alt_ds.stem}"
                                dst_alt_ds = doc_dir / f"{final_name}{suffix}{alt_ds.suffix}"
                                shutil.copy2(alt_ds, dst_alt_ds)

                    mb = get_setting_int(self.settings, "backups_to_keep", 5)
                    bw = get_setting_int(self.settings, "backup_window_minutes", 5)
                    
                    if hasattr(dialog.footprint_drop, 'is_library_link') and dialog.footprint_drop.is_library_link:
                        pass
                    elif dialog.footprint_drop.file_path:
                        src_fp = Path(dialog.footprint_drop.file_path)
                        dst_fp = fp_dir / f"{source_sym.name}.kicad_mod"
                        if src_fp.exists() and src_fp.resolve() != dst_fp.resolve():
                            shutil.copy2(src_fp, dst_fp)
                        source_sym.set_clean_property("Footprint", f"{target_cat_full}:{source_sym.name}")
                        
                        if dialog.model_drop.file_path:
                            src_mdl = Path(dialog.model_drop.file_path)
                            dst_mdl = model_dir / f"{source_sym.name}{src_mdl.suffix}"
                            if src_mdl.exists() and src_mdl.resolve() != dst_mdl.resolve():
                                shutil.copy2(src_mdl, dst_mdl)
                            
                            source_sym.set_clean_property("3D_Model", AssetManager.get_path_string(self.settings, lib_root, dst_mdl))
                            
                            mdl_path_str = AssetManager.get_path_string(self.settings, lib_root, dst_mdl)
                            with open(dst_fp, 'r', encoding='utf-8') as f: fp_content = f.read()
                            if '(model "' in fp_content: fp_content = re.sub(r'\(model\s+"[^"]+"', f'(model "{mdl_path_str}"', fp_content)
                            else:
                                last_paren = fp_content.rfind(')')
                                if last_paren != -1: fp_content = fp_content[:last_paren] + f'\n  (model "{mdl_path_str}"\n    (offset (xyz 0 0 0))\n    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n' + fp_content[last_paren:]
                            with open(dst_fp, 'w', encoding='utf-8') as f: f.write(fp_content)
                            
                    if dialog.datasheet_drop.file_path:
                        src_ds = Path(dialog.datasheet_drop.file_path)
                        dst_ds = doc_dir / f"{source_sym.name}{src_ds.suffix}"
                        if src_ds.exists() and src_ds.resolve() != dst_ds.resolve():
                            shutil.copy2(src_ds, dst_ds)
                        source_sym.set_clean_property("Datasheet", AssetManager.get_path_string(self.settings, lib_root, dst_ds))
                        
                    if source_sym.properties.get("Image_File") and Path(source_sym.properties["Image_File"]).exists():
                        src_img = Path(source_sym.properties["Image_File"])
                        dst_img = img_dir / f"{source_sym.name}{src_img.suffix}"
                        if src_img.exists() and src_img.resolve() != dst_img.resolve(): 
                            shutil.copy2(src_img, dst_img)
                        try: source_sym.set_clean_property("Image_File", str(dst_img.relative_to(lib_root)).replace('\\', '/'))
                        except ValueError: pass
                        
                except Exception as e:
                    return "ERROR", source_sym.name, f"Failed copying assets: {e}"

                try:
                    source_sym.raw_graphics_block = KiCadIO.rename_symbol_in_block(source_sym.raw_graphics_block, original_name, source_sym.name)
                    
                    if source_sym.category not in self.app_library.categories:
                        sym_dir = self.app_library.get_symbols_dir()
                        if sym_dir is not None:
                            new_path = sym_dir / f"{source_sym.category}.kicad_sym"
                            self.app_library.categories[source_sym.category] = Category(source_sym.category, new_path)
                        
                    target_cat_obj = self.app_library.categories[source_sym.category]
                    target_cat_obj.add_symbol(source_sym)
                    
                    KiCadIO.write_category(target_cat_obj, mb, bw)
                    if dup_sym and dup_sym.category != source_sym.category:
                        cat_dup_obj = self.app_library.categories.get(dup_sym.category)
                        if cat_dup_obj:
                            cat_dup_obj.remove_symbol_by_name(dup_sym.name)
                            KiCadIO.write_category(cat_dup_obj, mb, bw)
                            if not cat_dup_obj.symbols and cat_dup_obj.filepath and cat_dup_obj.filepath.exists():
                                try: cat_dup_obj.filepath.unlink()
                                except OSError: pass
                        
                    self.cached_part_names.add(source_sym.name)
                    return "SUCCESS", source_sym.name, ""
                except Exception as e:
                    return "ERROR", source_sym.name, f"Failed writing to library: {e}"
            else:
                if getattr(dialog, 'abort_batch', False): return "ABORT", source_sym.name, "User aborted."
                return "SKIPPED", source_sym.name, "User cancelled."

    def cleanup_originals(self, dropped_files):
        if get_setting_bool(self.settings, "delete_originals", False):
            for df in dropped_files:
                path_to_delete = Path(df)
                if path_to_delete.exists():
                    try:
                        if path_to_delete.is_file(): path_to_delete.unlink()
                        elif path_to_delete.is_dir(): shutil.rmtree(path_to_delete)
                    except Exception as e:
                        logger.warning(f"Could not delete {path_to_delete.name}: {e}")

    def import_legacy_library(self):
        if not self.app_library.is_valid(): return
        folder_path = QFileDialog.getExistingDirectory(self.view, "Select Legacy Library Root to Import")
        if not folder_path: return
        
        reply = QMessageBox.question(self.view, "Import Legacy Library", f"Are you sure you want to scan '{Path(folder_path).name}' for KiCad assets?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.process_dropped_files([folder_path])

    def relocate_library(self):
        old_root_str = get_setting_str(self.settings, "library_root", "")
        if not old_root_str:
            QMessageBox.warning(self.view, "No Library Configured", "You don't have a library configured yet.")
            self.view.open_settings()
            return
            
        old_root = Path(old_root_str)
        
        msg = ("Select the NEW folder for your library.\n\nThe manager will move all your assets (Symbols, Footprints, 3D Models, etc.) to this new location and automatically update all internal component links.")
        QMessageBox.information(self.view, "Relocate / Rename Library", msg)
        start_dir = str(old_root.parent) if old_root.exists() else os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self.view, "Select New Library Root Folder", start_dir)
        
        if not folder: return
        
        new_root = Path(folder)
        if new_root.resolve() == old_root.resolve(): return
        
        # 1. Physically Move Files Safely
        if old_root.exists():
            try:
                for folder_name in ["Symbols", "Footprints", "3D_Models", "Datasheets", "Images", ".bak"]:
                    src = old_root / folder_name
                    dst = new_root / folder_name
                    if src.exists():
                        self.view.set_working(True, f"Copying {folder_name} to new location...")
                        QApplication.processEvents()
                        if not dst.exists():
                            shutil.copytree(str(src), str(dst))
                        else:
                            for root_dir, dirs, files in os.walk(str(src)):
                                rel_path = os.path.relpath(root_dir, str(src))
                                target_dir = dst / rel_path
                                target_dir.mkdir(parents=True, exist_ok=True)
                                for file in files:
                                    shutil.copy2(os.path.join(root_dir, file), str(target_dir / file))
                                    
                # Cleanup old folders after a successful copy
                self.view.set_working(True, "Cleaning up old location...")
                QApplication.processEvents()
                for folder_name in ["Symbols", "Footprints", "3D_Models", "Datasheets", "Images", ".bak"]:
                    src = old_root / folder_name
                    if src.exists():
                        try:
                            shutil.rmtree(str(src))
                        except OSError as e:
                            logger.warning(f"Could not delete old folder {src}: {e}")
            except Exception as e:
                QMessageBox.critical(self.view, "Error", f"Failed to move files: {e}")
                self.view.set_working(False, "Ready")
                return
                
        # 2. Update Internal Links (Safely handle both Windows and Unix slash formats)
        self.view.set_working(True, "Updating internal asset links...")
        QApplication.processEvents()
        
        old_uri_base_fwd = str(old_root).replace('\\', '/')
        new_uri_base_fwd = str(new_root).replace('\\', '/')
        old_uri_base_back = str(old_root).replace('\\', '\\\\')
        
        # Update Symbols
        sym_dir = new_root / "Symbols"
        if sym_dir.exists():
            for sym_file in sym_dir.glob("*.kicad_sym"):
                try:
                    with open(sym_file, 'r', encoding='utf-8') as f: content = f.read()
                    if old_uri_base_fwd in content or old_uri_base_back in content:
                        content = content.replace(old_uri_base_fwd, new_uri_base_fwd)
                        content = content.replace(old_uri_base_back, new_uri_base_fwd)
                        with open(sym_file, 'w', encoding='utf-8') as f: f.write(content)
                except Exception as e:
                    logger.error(f"Failed to update links in {sym_file.name}: {e}")
                    
        # Update Footprints
        fp_root = new_root / "Footprints"
        if fp_root.exists():
            for fp_file in fp_root.rglob("*.kicad_mod"):
                try:
                    with open(fp_file, 'r', encoding='utf-8') as f: content = f.read()
                    if old_uri_base_fwd in content or old_uri_base_back in content:
                        content = content.replace(old_uri_base_fwd, new_uri_base_fwd)
                        content = content.replace(old_uri_base_back, new_uri_base_fwd)
                        with open(fp_file, 'w', encoding='utf-8') as f: f.write(content)
                except Exception as e:
                    logger.error(f"Failed to update links in {fp_file.name}: {e}")
        
        self.settings.setValue("library_root", str(new_root))
        self.app_library.set_root_path(str(new_root))
        
        reply = QMessageBox.question(self.view, 'Update KiCad Paths?', "Library assets successfully moved and internal links updated!\n\nDo you want to automatically update your KiCad global library tables to point to this new location?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if reply == QMessageBox.StandardButton.Yes:
            self.view.set_working(True, "Updating KiCad paths...")
            QApplication.processEvents()
            self.update_kicad_paths(str(old_root), str(new_root))
            
        self.view.set_working(False, "Ready")
        self.reload_library(preserve_state=False)

    def update_kicad_paths(self, old_root, new_root):
        saved_config = get_setting_str(self.settings, "kicad_config_path", "")
        config_dir = Path(saved_config) if saved_config else None
        
        if not config_dir or not config_dir.exists() or not (config_dir / "sym-lib-table").exists():
            system = platform.system()
            if system == "Windows": base_path = Path(os.environ.get("APPDATA", "")) / "kicad"
            elif system == "Darwin": base_path = Path.home() / "Library" / "Preferences" / "kicad"
            else: base_path = Path.home() / ".config" / "kicad"
            base_path = base_path if base_path.exists() else Path.home()

            folder = QFileDialog.getExistingDirectory(self.view, "Select KiCad Configuration Folder (Contains sym-lib-table)", str(base_path))
            if not folder: return
            config_dir = Path(folder)
            self.settings.setValue("kicad_config_path", str(config_dir))

        sym_table_path = config_dir / "sym-lib-table"
        fp_table_path = config_dir / "fp-lib-table"
        kicad_common_path = config_dir / "kicad_common.json"

        if not sym_table_path.exists() and not fp_table_path.exists():
            QMessageBox.critical(self.view, "Not Found", f"Could not find tables in {config_dir}")
            return

        old_uri_base = str(Path(old_root)).replace('\\', '/')
        new_uri_base = str(Path(new_root)).replace('\\', '/')
        updated_files = 0
        
        self.view.set_working(True, "Updating KiCad global tables...")
        QApplication.processEvents()
        
        for table_path in [sym_table_path, fp_table_path]:
            if table_path.exists():
                try:
                    with open(table_path, 'r', encoding='utf-8') as f: content = f.read()
                    if old_uri_base in content:
                        shutil.copy2(table_path, table_path.with_suffix('.bak_custom_mgr_relocate'))
                        new_content = content.replace(old_uri_base, new_uri_base)
                        with open(table_path, 'w', encoding='utf-8') as f: f.write(new_content)
                        updated_files += 1
                except PermissionError:
                    QMessageBox.warning(self.view, "File Locked", "Permission denied. Close KiCad and try again.")
                    return
                except Exception as e:
                    QMessageBox.critical(self.view, "Error", f"Failed updating {table_path.name}:\n{e}")

        # Update environment variable as well
        path_var_raw = get_setting_str(self.settings, "kicad_path_var", "").strip()
        path_var_name = path_var_raw.replace("${", "").replace("}", "")
        if path_var_name and kicad_common_path.exists():
            try:
                self.view.set_working(True, "Updating KiCad environment variables...")
                QApplication.processEvents()
                with open(kicad_common_path, 'r', encoding='utf-8') as f: kicad_config = json.load(f)
                if "environment" in kicad_config and "vars" in kicad_config["environment"]:
                    if kicad_config["environment"]["vars"].get(path_var_name) == str(Path(old_root)):
                        kicad_config["environment"]["vars"][path_var_name] = str(Path(new_root))
                        shutil.copy2(kicad_common_path, kicad_common_path.with_suffix('.bak_custom_mgr_relocate'))
                        with open(kicad_common_path, 'w', encoding='utf-8') as f: json.dump(kicad_config, f, indent=2)
                        updated_files += 1
            except Exception as e:
                logger.error(f"Failed updating environment variables: {e}")

        if updated_files > 0: QMessageBox.information(self.view, "Success", "Successfully updated library paths in KiCad configuration tables!")
        else: QMessageBox.information(self.view, "No Changes", "No matching old paths were found in KiCad configuration.")

    def add_to_kicad(self):
        if not self.app_library.is_valid(): return
        
        saved_config = get_setting_str(self.settings, "kicad_config_path", "")
        config_dir = Path(saved_config) if saved_config else None
        
        if not config_dir or not config_dir.exists() or not (config_dir / "sym-lib-table").exists():
            system = platform.system()
            if system == "Windows": base_path = Path(os.environ.get("APPDATA", "")) / "kicad"
            elif system == "Darwin": base_path = Path.home() / "Library" / "Preferences" / "kicad"
            else: base_path = Path.home() / ".config" / "kicad"
            base_path = base_path if base_path.exists() else Path.home()

            folder = QFileDialog.getExistingDirectory(self.view, "Select KiCad Configuration Folder (Contains sym-lib-table)", str(base_path))
            if not folder: return
            config_dir = Path(folder)
            self.settings.setValue("kicad_config_path", str(config_dir))

        sym_table_path = config_dir / "sym-lib-table"
        fp_table_path = config_dir / "fp-lib-table"
        kicad_common_path = config_dir / "kicad_common.json"

        self.view.set_working(True, "Injecting libraries into KiCad...")
        QApplication.processEvents()
        added_syms, added_fps, added_var = 0, 0, False

        # 1. Update the Environment Variable
        path_var_raw = get_setting_str(self.settings, "kicad_path_var", "").strip()
        path_var_name = path_var_raw.replace("${", "").replace("}", "")
        lib_root = self.app_library.root_path
        
        if path_var_name and lib_root and kicad_common_path.exists():
            try:
                with open(kicad_common_path, 'r', encoding='utf-8') as f: kicad_config = json.load(f)
                    
                if "environment" not in kicad_config: kicad_config["environment"] = {}
                if "vars" not in kicad_config["environment"]: kicad_config["environment"]["vars"] = {}
                    
                current_val = kicad_config["environment"]["vars"].get(path_var_name)
                if current_val != str(lib_root):
                    kicad_config["environment"]["vars"][path_var_name] = str(lib_root)
                    shutil.copy2(kicad_common_path, kicad_common_path.with_suffix('.bak_custom_mgr'))
                    with open(kicad_common_path, 'w', encoding='utf-8') as f: json.dump(kicad_config, f, indent=2)
                    added_var = True
            except Exception as e:
                logger.error(f"Failed to update KiCad environment variables: {e}")

        # 2. Update the Symbol Table
        if sym_table_path.exists():
            try:
                with open(sym_table_path, 'r', encoding='utf-8') as f: sym_content = f.read()
                sym_dir = self.app_library.get_symbols_dir()
                new_syms = []
                if sym_dir is not None and sym_dir.exists():
                    for sym_file in sym_dir.glob("*.kicad_sym"):
                        lib_name = sym_file.stem
                        if f'(name "{lib_name}")' not in sym_content:
                            uri = str(sym_file).replace('\\', '/')
                            new_syms.append(f'  (lib (name "{lib_name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n')
                if new_syms:
                    last_paren = sym_content.rfind(')')
                    if last_paren != -1:
                        sym_content = sym_content[:last_paren] + "".join(new_syms) + sym_content[last_paren:]
                        shutil.copy2(sym_table_path, sym_table_path.with_suffix('.bak_custom_mgr'))
                        with open(sym_table_path, 'w', encoding='utf-8') as f: f.write(sym_content)
                        added_syms = len(new_syms)
            except Exception as e:
                self.view.set_working(False, "Ready")
                QMessageBox.critical(self.view, "Error", str(e))

        # 3. Update the Footprint Table
        if fp_table_path.exists():
            try:
                with open(fp_table_path, 'r', encoding='utf-8') as f: fp_content = f.read()
                if lib_root is not None:
                    fp_dir = lib_root / "Footprints"
                    new_fps = []
                    if fp_dir.exists():
                        for fp_folder in fp_dir.glob("*.pretty"):
                            lib_name = fp_folder.stem
                            if f'(name "{lib_name}")' not in fp_content:
                                uri = str(fp_folder).replace('\\', '/')
                                new_fps.append(f'  (lib (name "{lib_name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n')
                    if new_fps:
                        last_paren = fp_content.rfind(')')
                        if last_paren != -1:
                            fp_content = fp_content[:last_paren] + "".join(new_fps) + fp_content[last_paren:]
                            shutil.copy2(fp_table_path, fp_table_path.with_suffix('.bak_custom_mgr'))
                            with open(fp_table_path, 'w', encoding='utf-8') as f: f.write(fp_content)
                            added_fps = len(new_fps)
            except Exception as e:
                self.view.set_working(False, "Ready")
                QMessageBox.critical(self.view, "Error", str(e))

        self.view.set_working(False, "Ready")
        
        if added_syms == 0 and added_fps == 0 and not added_var:
            QMessageBox.information(self.view, "Up to Date", "Your KiCad library tables and variables are already fully up to date!")
        else:
            msg = f"Linked Custom Libraries!\n\nNew Symbols: {added_syms}\nNew Footprints: {added_fps}"
            if added_var: msg += f"\nEnvironment Variable Mapped: {path_var_name}"
            QMessageBox.information(self.view, "Success", msg)

    def restore_backup(self):
        lib_root = self.get_active_library_root(show_warning=True)
        if not lib_root: return
        
        bak_sym_dir = lib_root / ".bak" / "Symbols"
        if not bak_sym_dir.exists():
            QMessageBox.information(self.view, "No Backups", "No backup files found in the library.")
            return
            
        backups = list(bak_sym_dir.glob("*.bak_*"))
        if not backups:
            QMessageBox.information(self.view, "No Backups", "No backup files found in the library.")
            return

        def generate_diff(backup_path: Path):
            base_name = backup_path.name.split('.bak')[0]
            current_path = lib_root / "Symbols" / base_name
            
            backup_cat = KiCadIO.parse_category(backup_path)
            current_cat = KiCadIO.parse_category(current_path) if current_path.exists() else None
            
            backup_syms = {s.name: s for s in backup_cat.symbols} if backup_cat else {}
            current_syms = {s.name: s for s in current_cat.symbols} if current_cat else {}
            
            b_names = set(backup_syms.keys())
            c_names = set(current_syms.keys())
            
            restored = b_names - c_names
            lost = c_names - b_names
            shared = b_names.intersection(c_names)
            
            edited = []
            for sym_name in shared:
                if backup_syms[sym_name].properties != current_syms[sym_name].properties:
                    edited.append(sym_name)
                    
            html = f"<h3>Target Library: {base_name}</h3>"
            if restored:
                html += f"<h4 style='color: #0F9D58;'>Will be Restored ({len(restored)} parts):</h4><ul>"
                for s in sorted(restored): html += f"<li>{s}</li>"
                html += "</ul>"
            if lost:
                html += f"<h4 style='color: #F14C4C;'>Will be Lost ({len(lost)} parts):</h4><ul>"
                for s in sorted(lost): html += f"<li>{s}</li>"
                html += "</ul>"
            if edited:
                html += f"<h4 style='color: #FD7E14;'>Properties Reverted ({len(edited)} parts):</h4><ul>"
                for s in sorted(edited): html += f"<li>{s}</li>"
                html += "</ul>"
            if not restored and not lost and not edited:
                html += "<p>No structural or property differences found between this backup and the current library file.</p>"
                
            return html

        dialog = BackupRestoreDialog(self.view, backups, generate_diff)
        if dialog.exec() == int(QDialog.DialogCode.Accepted):
            selected = dialog.selected_backup
            if selected:
                base_name = selected.name.split('.bak')[0]
                current_path = selected.parent / base_name
                
                if current_path.exists() and hasattr(KiCadIO, 'backup_file'):
                    KiCadIO.backup_file(current_path, 5, 0)
                    
                shutil.copy2(selected, current_path)
                self.reload_library(preserve_state=True)
                QMessageBox.information(self.view, "Restored", f"Successfully restored '{base_name}' from backup!")
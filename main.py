"""
main.py
The unified application entry point.
Acts as the View layer, hosting the Library Browser, Drop Zone, and Blueprint Viewer.
"""
import sys
import logging
import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (QApplication, QMainWindow, QLabel, 
                               QMessageBox, QProgressBar, QWidget, 
                               QVBoxLayout, QHBoxLayout, QPushButton,
                               QSplitter, QTabWidget, QAbstractItemView, QDialog,
                               QTreeWidgetItem, QHeaderView)
from PySide6.QtCore import Qt, QSettings, QTimer, QUrl
from PySide6.QtGui import QAction, QIcon, QDesktopServices

from ui_views import (MainDropZone, SettingsDialog, ManualDialog, 
                      LogViewerDialog, DigestDialog, BackupRestoreDialog,
                      get_setting_str, get_setting_bool, KiCadViewerWidget,
                      NoDeselectTreeWidget)
from library_manager import LibraryController
from models import AppTheme
from constants import ABOUT_HTML

LOG_FILE_PATH = Path.home() / ".kicad_lib_manager" / "logs" / "app.log"

def setup_logging():
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    fh = logging.FileHandler(LOG_FILE_PATH, mode='w', encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    if not getattr(sys, 'frozen', False):
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Uncaught Exception:", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception
    return logger

logger = setup_logging()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("KiCad Custom Library Manager")
        self.resize(1300, 640) 
        
        self.settings = QSettings("OpenSourceTools", "KiCadLibManager")
        
        app_inst = QApplication.instance()
        if isinstance(app_inst, QApplication):
            current_theme = get_setting_str(self.settings, "theme", "Modern Light")
            app_inst.setStyle("Fusion")
            app_inst.setPalette(AppTheme.get_palette(current_theme, app_inst.style().standardPalette()))
            app_inst.setStyleSheet(AppTheme.get_stylesheet(current_theme))
        
        # Link View to Controller
        self.controller = LibraryController(self)
        
        self.init_ui()
        self.init_menu()
        self.apply_window_settings()
        
        QTimer.singleShot(100, self.controller.check_startup)

    def apply_window_settings(self):
        always_on_top = get_setting_bool(self.settings, "always_on_top", False)
        if always_on_top: self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        else: self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.show()

    def init_ui(self):
        central_container = QWidget()
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(10, 10, 10, 10)
        
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        central_layout.addWidget(self.splitter)
        
        # LEFT PANE: Drop Zone
        self.left_panel = QWidget()
        self.left_panel.setMaximumWidth(260)
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        self.drop_zone = MainDropZone()
        self.drop_zone.filesDropped.connect(self.controller.process_dropped_files)
        left_layout.addWidget(self.drop_zone)
        
        # MIDDLE PANE: Browser / Scanner
        self.mid_panel = QWidget()
        mid_layout = QVBoxLayout(self.mid_panel)
        mid_layout.setContentsMargins(0, 0, 0, 0)
        
        self.tabs = QTabWidget()
        self.browser_tab = QWidget()
        self.health_tab = QWidget()
        self.report_tab = QWidget()

        self.setup_browser_tab()
        self.setup_health_tab()
        self.setup_report_tab()
        
        self.tabs.addTab(self.browser_tab, "Library Browser")
        self.tabs.addTab(self.health_tab, "Health Scanner")
        self.tabs.addTab(self.report_tab, "Library Report")
        mid_layout.addWidget(self.tabs)
        
        btn_layout = QHBoxLayout()
        self.btn_delete = QPushButton("Delete Selected")
        self.btn_delete.setProperty("action", "danger")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self.controller.delete_selected_parts)
        
        self.btn_edit = QPushButton("Edit Component")
        self.btn_edit.setEnabled(False)
        self.btn_edit.clicked.connect(self.controller.edit_selected_part)
        
        self.btn_resolve = QPushButton("🛠 Resolve Issue")
        self.btn_resolve.setProperty("action", "primary")
        self.btn_resolve.setEnabled(False)
        self.btn_resolve.setVisible(False)
        self.btn_resolve.clicked.connect(self.controller.resolve_selected_issues)
        
        self.btn_open_loc = QPushButton("📂 Open Location")
        self.btn_open_loc.setEnabled(False)
        self.btn_open_loc.setVisible(False)
        self.btn_open_loc.clicked.connect(self.controller.open_selected_location)
        
        self.btn_view_sym = QPushButton("👁 View Symbol")
        self.btn_view_sym.setEnabled(False)
        self.btn_view_sym.clicked.connect(lambda: self.view_symbol(silent_fail=False))
        
        self.btn_view_fp = QPushButton("👁 View Footprint")
        self.btn_view_fp.setEnabled(False)
        self.btn_view_fp.clicked.connect(lambda: self.view_footprint(silent_fail=False))
        
        self.btn_merge = QPushButton("Merge Selected")
        self.btn_merge.setEnabled(False)
        self.btn_merge.clicked.connect(self.controller.merge_selected_parts)
        
        btn_layout.addWidget(self.btn_delete)
        btn_layout.addWidget(self.btn_edit)
        btn_layout.addWidget(self.btn_resolve)
        btn_layout.addWidget(self.btn_open_loc)
        btn_layout.addWidget(self.btn_view_sym)
        btn_layout.addWidget(self.btn_view_fp)
        btn_layout.addWidget(self.btn_merge)
        btn_layout.addStretch()
        
        self.tabs.currentChanged.connect(self.on_tab_changed)
        mid_layout.addLayout(btn_layout)
        
        # RIGHT PANE: Viewer
        self.viewer_panel = KiCadViewerWidget()
        self.viewer_panel.setVisible(False)
        self.viewer_panel.preview_closed.connect(self.close_viewer)
        
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.mid_panel)
        self.splitter.addWidget(self.viewer_panel)
        
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 6)
        self.splitter.setStretchFactor(2, 4)
        
        self.setCentralWidget(central_container)
        
        self.status_bar = self.statusBar()
        self.status_lbl = QLabel("Ready")
        self.status_bar.addWidget(self.status_lbl)
        
        self.working_spinner = QProgressBar()
        self.working_spinner.setRange(0, 0)
        self.working_spinner.setFixedSize(120, 12)
        self.working_spinner.hide()
        self.status_bar.addPermanentWidget(self.working_spinner)

    def set_working(self, is_working: bool, text: str = "Ready"):
        self.status_lbl.setText(text)
        self.working_spinner.setVisible(is_working)
        QApplication.processEvents()

    def setup_browser_tab(self):
        layout = QVBoxLayout(self.browser_tab)
        layout.addWidget(QLabel("<b>Library Browser</b><br><small>View and manage all categories and components in your library.</small>"))
        self.browser_tree = NoDeselectTreeWidget()
        self.browser_tree.setHeaderLabels(["Part Name", "Manufacturer", "MPN", "Description"])
        self.browser_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.browser_tree.itemSelectionChanged.connect(lambda: self.controller.on_selection_changed(self.browser_tree))
        
        self.browser_tree.itemDoubleClicked.connect(self.controller.edit_selected_part)
        
        layout.addWidget(self.browser_tree)

    def populate_browser_tree(self):
        """Populates the main library browser tree with categories and symbols."""
        # 1. Store the currently selected item names BEFORE clearing the tree
        selected_names = [item.text(0) for item in self.browser_tree.selectedItems()]
        
        self.browser_tree.clear()
        
        # Safely get the library data from the controller
        app_lib = getattr(self.controller, 'app_library', None)
        if not app_lib:
            return
            
        categories = getattr(app_lib, 'categories', {})
        
        # Alphabetize Categories
        if isinstance(categories, dict):
            cat_items = sorted(categories.items(), key=lambda x: str(x[0]).lower())
        else:
            cat_items = enumerate(categories)
        
        for key, cat_obj in cat_items:
            # Handle varying data structures gracefully
            if isinstance(cat_obj, list):
                cat_name = str(key)
                sym_list = cat_obj
            else:
                cat_name = getattr(cat_obj, 'name', str(key))
                symbols = getattr(cat_obj, 'symbols', [])
                sym_list = symbols.values() if isinstance(symbols, dict) else symbols

            cat_item = QTreeWidgetItem([cat_name, "", "", ""])
            
            # Make the category folder bold for better visibility
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            
            # Alphabetize Symbols within Category
            sym_list = sorted(list(sym_list), key=lambda s: getattr(s, 'name', 'Unknown').lower())
            
            for sym in sym_list:
                name = getattr(sym, 'name', 'Unknown')
                props = getattr(sym, 'properties', {})
                
                mfg = props.get("Manufacturer", "")
                mpn = props.get("MPN", "")
                desc = props.get("Description", "")
                
                sym_item = QTreeWidgetItem([name, str(mfg), str(mpn), str(desc)])
                # Store the raw symbol object inside the tree item so the Controller can retrieve it
                sym_item.setData(0, Qt.ItemDataRole.UserRole, sym)
                cat_item.addChild(sym_item)
                
                # 2. Reselect the items AFTER repopulating the tree
                if name in selected_names:
                    sym_item.setSelected(True)
                    cat_item.setExpanded(True)
                    
            self.browser_tree.addTopLevelItem(cat_item)
            
        self.browser_tree.resizeColumnToContents(0)
        self.browser_tree.resizeColumnToContents(1)

    def setup_health_tab(self):
        layout = QVBoxLayout(self.health_tab)
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("<b>Library Health Scanner</b><br><small>Detects duplicates, similarly named symbols, and other data inconsistencies.</small>"))
        top_layout.addStretch()
        self.refresh_btn = QPushButton("🔄 Rescan Library")
        self.refresh_btn.setProperty("action", "primary")
        self.refresh_btn.clicked.connect(lambda: self.controller.reload_library(preserve_state=True))
        top_layout.addWidget(self.refresh_btn)
        layout.addLayout(top_layout)
        
        self.health_tree = NoDeselectTreeWidget()
        
        # Updated Columns to exactly match the requested schema
        self.health_tree.setHeaderLabels(["File Name", "Current Category", "Issue", "Recommended Fix"])
        self.health_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.health_tree.setWordWrap(True) # Wraps text when descriptions get too long!
        
        self.health_tree.itemSelectionChanged.connect(lambda: self.controller.on_selection_changed(self.health_tree))
        self.health_tree.itemDoubleClicked.connect(self.controller.edit_selected_part)
        
        layout.addWidget(self.health_tree)
        
    def populate_health_tree(self):
        """Populates the health tree view after a scan."""
        self.health_tree.clear()
        
        # Safely get the scan results from the controller.
        health_data = getattr(self.controller, 'health_issues', [])
        
        # Update Tab Title with issue count
        issue_count = len(health_data)
        tab_idx = self.tabs.indexOf(self.health_tab)
        if issue_count > 0:
            self.tabs.setTabText(tab_idx, f"Health Scanner ({issue_count})")
        else:
            self.tabs.setTabText(tab_idx, "Health Scanner")
        
        if not health_data:
            item = QTreeWidgetItem(["✅ No issues detected", "N/A", "Your library is perfectly healthy.", ""])
            self.health_tree.addTopLevelItem(item)
            return

        for issue in health_data:
            if isinstance(issue, dict):
                i_type = issue.get('type', '')
                parts = issue.get('parts', [])
                
                if i_type == 'Duplicate / Conflict Group':
                    name = f"Multiple Parts ({len(parts)})"
                    cat = parts[0].get('category', 'Mixed') if parts else 'Unknown'
                    if cat.startswith("Custom_"):
                        cat = cat.replace("Custom_", "", 1)
                        
                    details = issue.get('desc', '').replace(' | ', ', ')
                    fix = "Select both parts and click Merge"
                    
                elif i_type == 'Broken Link':
                    sym = parts[0].get('sym_obj') if parts else None
                    name = sym.name if sym else "Unknown Symbol"
                    
                    cat = sym.category if sym else "Unknown"
                    if cat.startswith("Custom_"):
                        cat = cat.replace("Custom_", "", 1)
                        
                    details = issue.get('desc', 'Broken file reference')
                    fix = "Auto-heal link, or clear reference"
                    
                else:
                    # Consolidated File Issues (Wrong Category, Orphaned Asset, etc)
                    path_raw = parts[0].get('path') if parts else None
                    path = Path(path_raw) if path_raw else None
                    
                    # 1. Explicitly isolate File Name vs Category Location
                    if path:
                        name = path.name
                        cat = path.parent.name.replace('.pretty', '')
                    else:
                        name = issue.get('desc', 'Unknown File')
                        cat = parts[0].get('category', 'Unknown') if parts else 'Unknown'
                        if cat.startswith("Custom_"):
                            cat = cat.replace("Custom_", "", 1)
                            
                    # 2. Extract issue description reliably regardless of controller grouping
                    raw_details = parts[0].get('name', 'Needs attention') if parts else 'Needs attention'
                    
                    if " | " in raw_details or "Footprint" in raw_details or "Model" in raw_details or "Datasheet" in raw_details:
                        details = raw_details.replace(" | ", ",\n")
                    else:
                        details = issue.get('desc', 'Needs attention')
                        
                    # 3. Determine fix based on specific issue flag
                    if parts and 'expected_cat' in parts[0]:
                        fix = f"Move to '{parts[0]['expected_cat']}' category"
                    elif parts and 'orphan' in parts[0].get('issue_type', ''):
                        fix = "Link to matching symbol, or Delete"
                    else:
                        fix = "Resolve file issue"
            else:
                name = getattr(issue, 'name', 'Unknown Part')
                cat = getattr(issue, 'category', 'Uncategorized')
                details = getattr(issue, 'details', 'Needs attention')
                fix = "Review item"
                
            item = QTreeWidgetItem([name, cat, details, fix])
            item.setData(0, Qt.ItemDataRole.UserRole, issue)
            self.health_tree.addTopLevelItem(item)
            
        # Shrink Col 0 and Col 1 to tightly fit their contents, freeing space for wrapped text
        self.health_tree.resizeColumnToContents(0)
        self.health_tree.resizeColumnToContents(1)
        
        # Stretch columns 2 and 3 fully to accommodate wrapped text
        self.health_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.health_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def setup_report_tab(self):
        """Initializes the report tab and its tree widget."""
        # 1. Set up the layout for the Report Tab
        layout = QVBoxLayout(self.report_tab)
        
        # 2. Use the imported tree widget instead of a QTreeWidgetItem
        self.report_tree = NoDeselectTreeWidget()
        
        # 3. Apply your exact custom header labels, including an empty column at the end
        self.report_tree.setHeaderLabels(["Component", "Category", "Footprint", "3D Model", "Datasheet", "Image", ""])
        self.report_tree.setAlternatingRowColors(True)
        
        # 4. Bind the double-click event you already defined below
        self.report_tree.itemDoubleClicked.connect(self.on_report_item_double_clicked)
        
        # 5. Lock it into the UI
        layout.addWidget(self.report_tree)

    def populate_report_tree(self):
        """Builds a complete matrix of all components and their linked physical files."""
        self.report_tree.clear()
        if not hasattr(self.controller, 'app_library') or not self.controller.app_library.root_path: 
            return
            
        lib_root = Path(self.controller.app_library.root_path)
        prefix = get_setting_str(self.settings, "library_prefix", "Custom_")
        path_var_raw = get_setting_str(self.settings, "kicad_path_var", "").strip()
        all_syms = sorted(self.controller.app_library.get_all_symbols(), key=lambda s: s.name.lower())
        
        YES = "🟢"
        NO = "🔴"
        
        for sym in all_syms:
            cat_clean = sym.category[len(prefix):] if sym.category.startswith(prefix) else sym.category
            
            # --- Check Footprint ---
            has_fp = False
            fp_str = sym.get_clean_property("Footprint", "")
            if fp_str and ":" in fp_str:
                c, n = fp_str.split(":", 1)
                if (lib_root / "Footprints" / f"{prefix}{cat_clean}.pretty" / f"{n}.kicad_mod").exists() or \
                   (lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod").exists():
                    has_fp = True
            elif fp_str and Path(fp_str).exists():
                has_fp = True
                    
            # --- Check 3D Model ---
            has_mdl = False
            mdl_str = sym.get_clean_property("3D_Model", sym.get_clean_property("3D Model", ""))
            if mdl_str:
                mdl_clean = str(mdl_str)
                if path_var_raw and mdl_clean.startswith(path_var_raw):
                    mdl_clean = mdl_clean.replace(path_var_raw, "").lstrip("\\/")
                elif mdl_clean.startswith("${KIPRJMOD}"):
                    mdl_clean = mdl_clean.replace("${KIPRJMOD}", "").lstrip("\\/")
                
                m_path = Path(mdl_clean)
                if not m_path.is_absolute(): m_path = lib_root / m_path
                if m_path.exists(): has_mdl = True
                
            # --- Check Datasheet ---
            has_ds = False
            ds_str = sym.get_clean_property("Datasheet", "")
            if ds_str:
                if str(ds_str).startswith("http"): has_ds = True
                else:
                    ds_clean = str(ds_str)
                    if path_var_raw and ds_clean.startswith(path_var_raw):
                        ds_clean = ds_clean.replace(path_var_raw, "").lstrip("\\/")
                    elif ds_clean.startswith("${KIPRJMOD}"):
                        ds_clean = ds_clean.replace("${KIPRJMOD}", "").lstrip("\\/")
                        
                    d_path = Path(ds_clean)
                    if not d_path.is_absolute(): d_path = lib_root / d_path
                    if d_path.exists(): has_ds = True
                    
            # --- Check Image ---
            has_img = False
            img_str = sym.get_clean_property("Image_File", "")
            if img_str:
                if str(img_str).startswith("http"): has_img = True
                else:
                    i_path = Path(str(img_str))
                    if not i_path.is_absolute(): i_path = lib_root / i_path
                    if i_path.exists(): has_img = True
            
            # --- Generate the Tree Row ---
            item = QTreeWidgetItem([
                sym.name,
                cat_clean,
                YES if has_fp else NO,
                YES if has_mdl else NO,
                YES if has_ds else NO,
                YES if has_img else NO,
                "" # The empty string for the trailing blank column
            ])
            
            for i in range(2, 6):
                item.setTextAlignment(i, Qt.AlignmentFlag.AlignCenter)
            
            item.setData(0, Qt.ItemDataRole.UserRole, sym)
            self.report_tree.addTopLevelItem(item)

        # Auto-resize columns so it looks like a tidy spreadsheet
        for i in range(6):
            self.report_tree.resizeColumnToContents(i)

    def on_report_item_double_clicked(self, item, column):
        """Immediately loads the selected part into the Editor when double-clicked."""
        sym = item.data(0, Qt.ItemDataRole.UserRole)
        if not sym: return
        self.controller.selected_symbols = [sym]
        self.controller.edit_selected_part()

    def init_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('File')
        
        self.import_legacy_act = QAction('Import Directory...', self)
        self.import_legacy_act.triggered.connect(self.controller.import_legacy_library)
        file_menu.addAction(self.import_legacy_act)
        
        self.add_kicad_act = QAction('Add Libraries to KiCad...', self)
        self.add_kicad_act.triggered.connect(self.controller.add_to_kicad)
        file_menu.addAction(self.add_kicad_act)
        
        file_menu.addSeparator()
        file_menu.addAction('Relocate / Rename Library...', self.controller.relocate_library)
        file_menu.addAction('Restore from Backup...', self.controller.restore_backup)
        file_menu.addAction('Settings...', self.open_settings)
        file_menu.addSeparator()
        file_menu.addAction('Exit', self.close)

        help_menu = menubar.addMenu('Help')
        help_menu.addAction('Manual && Workflow...', self.show_manual)
        help_menu.addAction('View Logs...', self.view_logs)
        help_menu.addAction('Open Log Folder...', self.open_log_folder)
        help_menu.addSeparator()
        help_menu.addAction('About...', self.show_about)

    def open_settings(self):
        old_root = get_setting_str(self.settings, "library_root", "")
        dialog = SettingsDialog(self)
        if dialog.exec() == int(QDialog.DialogCode.Accepted):
            self.apply_window_settings()
            
            new_theme = get_setting_str(self.settings, "theme", "Modern Light")
            app_inst = QApplication.instance()
            if isinstance(app_inst, QApplication):
                app_inst.setPalette(AppTheme.get_palette(new_theme, app_inst.style().standardPalette()))
                app_inst.setStyleSheet(AppTheme.get_stylesheet(new_theme))
                
            new_root = get_setting_str(self.settings, "library_root", "")
            if new_root and Path(new_root).exists():
                self.controller.app_library.set_root_path(new_root)
                
                # Dynamically re-import so we avoid cyclic API references at startup
                from api import DigiKeyAPI
                self.controller.dk_api = DigiKeyAPI()
                
                if old_root and new_root and old_root != new_root:
                    if QMessageBox.question(self, 'Update KiCad Paths?', "Library Root changed.\nUpdate global library tables?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                        self.set_working(True, "Updating paths...")
                        self.controller.update_kicad_paths(old_root, new_root)
                        self.set_working(False, "Ready")
                self.controller.reload_library(preserve_state=False)

    def show_digest(self, title, successes, skips, failures):
        if not successes and not skips and not failures: return
        theme = get_setting_str(self.settings, "theme", "Modern Light")
        dialog = DigestDialog(self, title, successes, skips, failures, theme)
        dialog.exec()

    def view_logs(self):
        dialog = LogViewerDialog(LOG_FILE_PATH, self)
        dialog.exec()

    def open_log_folder(self):
        log_dir = Path.home() / ".kicad_lib_manager" / "logs"
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))

    def show_manual(self):
        dialog = ManualDialog(self)
        dialog.exec()

    def show_about(self):
        QMessageBox.about(self, "About KiCad Library Manager", ABOUT_HTML)


    def on_tab_changed(self, index):
        if index == 1:
            self.btn_resolve.setVisible(True)
            self.btn_open_loc.setVisible(True)
            self.btn_edit.setVisible(False)
        else:
            self.btn_resolve.setVisible(False)
            self.btn_open_loc.setVisible(False)
            self.btn_edit.setVisible(True)

    def close_viewer(self):
        if self.viewer_panel.isVisible():
            self.viewer_panel.setVisible(False)
            self.resize(self.width() - 450, self.height())

    def view_symbol(self, silent_fail=False):
        if len(self.controller.selected_symbols) != 1: 
            if not self.viewer_panel.isVisible() and not silent_fail:
                w0 = self.left_panel.width()
                w1 = self.mid_panel.width()
                self.resize(self.width() + 450, self.height())
                self.viewer_panel.setVisible(True)
                self.splitter.setSizes([w0, w1, 450])
                
            if not silent_fail:
                self.set_working(False, "No Symbol associated with this selection to preview.")
                QTimer.singleShot(4000, lambda: self.set_working(False, "Ready"))
                
            if self.viewer_panel.isVisible():
                self.viewer_panel.clear()
                self.viewer_panel.title_lbl.setText("<b>Symbol Preview (No preview available)</b>")
            return
            
        sym = self.controller.selected_symbols[0]
        
        if not self.viewer_panel.isVisible():
            w0 = self.left_panel.width()
            w1 = self.mid_panel.width()
            self.resize(self.width() + 450, self.height())
            self.viewer_panel.setVisible(True)
            # Explicitly lock the panels to their old widths to prevent them from jumping!
            self.splitter.setSizes([w0, w1, 450])
            
        self.viewer_panel.clear()
        self.viewer_panel.draw_symbol(sym.raw_graphics_block)
        QTimer.singleShot(10, self.viewer_panel.fit_view)

    def view_footprint(self, silent_fail=False):
        fp_path = None
        fp_str = ""
        
        # Check if we have a direct path from an issue (like an orphaned footprint)
        if len(self.controller.selected_issues) == 1:
            issue = self.controller.selected_issues[0]
            i_type = issue.get('issue_type', '')
            if i_type in ['orphan_fp', 'wrong_cat_fp']:
                fp_path = issue.get('path')
                fp_str = str(fp_path.name) if fp_path else ""
                
        # Check if we have a symbol providing the footprint reference
        if not fp_path and len(self.controller.selected_symbols) == 1:
            sym = self.controller.selected_symbols[0]
            fp_str = sym.get_clean_property("Footprint", "")
            if fp_str and ":" in fp_str:
                cat, fp_name = fp_str.split(":", 1)
                if self.controller.app_library.root_path is not None:
                    fp_path = self.controller.app_library.root_path / "Footprints" / f"{cat}.pretty" / f"{fp_name}.kicad_mod"

        # Pop open the panel if not visible, and there is a valid file or we're loudly failing
        if not self.viewer_panel.isVisible() and (fp_path or not silent_fail):
            w0 = self.left_panel.width()
            w1 = self.mid_panel.width()
            self.resize(self.width() + 450, self.height())
            self.viewer_panel.setVisible(True)
            self.splitter.setSizes([w0, w1, 450])
            
        if not fp_str and not fp_path:
            if not silent_fail:
                self.set_working(False, "No Footprint mapped to this selection.")
                QTimer.singleShot(4000, lambda: self.set_working(False, "Ready"))
            if self.viewer_panel.isVisible():
                self.viewer_panel.clear()
                self.viewer_panel.title_lbl.setText("<b>Footprint Preview (No preview available)</b>")
            return
            
        if fp_path and fp_path.exists():
            self.viewer_panel.clear()
            self.viewer_panel.draw_footprint(str(fp_path), theme="standard")
            QTimer.singleShot(10, self.viewer_panel.fit_view)
            return
            
        # If we got here, we had a reference string but the file was missing
        if not silent_fail:
            self.set_working(False, f"Could not locate the physical file for '{fp_str}'.")
            QTimer.singleShot(4000, lambda: self.set_working(False, "Ready"))
            
        if self.viewer_panel.isVisible():
            self.viewer_panel.clear()
            self.viewer_panel.title_lbl.setText("<b>Footprint Preview (File Missing)</b>")

if __name__ == '__main__':
    # --- NEW: Windows Taskbar & Start Menu Icon Fix ---
    import os
    if os.name == 'nt':
        try:
            import ctypes
            # An arbitrary, unique string identifying this app
            myappid = 'opensourcetools.kicadlibmanager.app.1.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass
    # --------------------------------------------------

    # --- NEW: PyInstaller Resource Path Helper ---
    def resource_path(relative_path):
        """ Get absolute path to resource, works for dev and for PyInstaller """
        try:
            # PyInstaller creates a temp folder and stores path in _MEIPASS
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.abspath(".")
        return os.path.join(base_path, relative_path)
    # --------------------------------------------------

    app = QApplication(sys.argv)
    
    # Fast-load visual splash screen while engine parses
    from PySide6.QtWidgets import QSplashScreen
    from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
    
    splash_pix = QPixmap(450, 250)
    splash_pix.fill(QColor(30, 30, 30))
    painter = QPainter(splash_pix)
    
    # 1. Try to load icon to display on the splash screen
    icon_path = None
    png_path = Path(resource_path("icon.png"))
    ico_path = Path(resource_path("icon.ico"))
    
    if png_path.exists():
        icon_path = str(png_path)
    elif ico_path.exists():
        icon_path = str(ico_path)
        
    if icon_path:
        icon_pix = QPixmap(icon_path)
        if not icon_pix.isNull():
            # Scale icon nicely
            icon_pix = icon_pix.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            # Draw icon centered horizontally, near the top
            x_pos = (450 - icon_pix.width()) // 2
            painter.drawPixmap(x_pos, 40, icon_pix)
            
    # 2. Draw Application Title
    painter.setPen(QColor(0, 122, 204))
    painter.setFont(QFont("Arial", 18, QFont.Weight.Bold))
    
    # Adjust Y position of the text based on whether we drew an icon
    title_y = 140 if icon_path else 100
    painter.drawText(0, title_y, 450, 40, Qt.AlignmentFlag.AlignCenter, "KiCad Library Manager")
    
    # 3. Draw Subtitle / Status with a smaller, subtle font
    painter.setPen(QColor(150, 150, 150))
    painter.setFont(QFont("Arial", 10, QFont.Weight.Normal))
    painter.drawText(0, title_y + 40, 450, 30, Qt.AlignmentFlag.AlignCenter, "Starting Engine...")
    
    painter.end()
    
    splash = QSplashScreen(splash_pix, Qt.WindowType.WindowStaysOnTopHint)
    splash.show()
    app.processEvents()

    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    
    window = MainWindow()
    window.show()
    splash.finish(window)
    
    sys.exit(app.exec())
"""
models.py
Contains the central in-memory data structures (MVC Model) for the application, 
as well as the centralized UI Theme/CSS definitions to keep view controllers clean.
"""
import uuid
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ==========================================
# KICAD DATA MODELS
# ==========================================

class Pin:
    """Represents a single pin on a KiCad symbol."""
    def __init__(self, number: str, name: str, direction: str, style: str, at: str):
        self.number = number
        self.name = name
        self.direction = direction
        self.style = style
        self.at = at

class Symbol:
    """Represents a single KiCad component/symbol in memory."""
    def __init__(self, name: str, category: str):
        self.uuid = str(uuid.uuid4())
        self.name = name
        self.category = category
        
        self.properties: Dict[str, str] = {}
        self.pins: List[Pin] = []
        
        # We preserve the raw graphical S-expression block (rectangles, arcs, etc.)
        # so that when we write the symbol back out, the graphics remain 100% untouched.
        self.raw_graphics_block: str = ""

    def get_clean_property(self, key: str, default: str = "") -> str:
        """Safely fetches a property, removing KiCad's string escapes if present."""
        val = self.properties.get(key, default)
        return val.replace('\\"', '"').replace('\\\\', '\\')

    def set_clean_property(self, key: str, value: str):
        """Sets a property, ensuring it is properly escaped for KiCad S-expressions."""
        # Sanitize newlines that break KiCad's single-line string parser
        clean_val = str(value).replace('\n', ' ').replace('\r', '').strip()
        self.properties[key] = clean_val


class Category:
    """Represents a .kicad_sym file containing multiple symbols."""
    def __init__(self, name: str, filepath: Path):
        self.name = name
        self.filepath = filepath
        self.symbols: List[Symbol] = []
        
    def add_symbol(self, symbol: Symbol):
        self.symbols.append(symbol)
        
    def remove_symbol_by_name(self, name: str) -> bool:
        initial_count = len(self.symbols)
        self.symbols = [s for s in self.symbols if s.name != name]
        return len(self.symbols) < initial_count
        
    def get_symbol(self, name: str) -> Optional[Symbol]:
        for s in self.symbols:
            if s.name == name:
                return s
        return None


class Library:
    """The Single Source of Truth for the entire loaded KiCad Custom Library."""
    def __init__(self):
        self._root_path: Optional[Path] = None
        self.categories: Dict[str, Category] = {}
        
    @property
    def root_path(self) -> Optional[Path]:
        return self._root_path

    def set_root_path(self, path_str: str) -> bool:
        """Validates and sets the library root. Returns True if valid."""
        if not path_str:
            self._root_path = None
            return False
            
        p = Path(path_str)
        if p.exists() and p.is_dir():
            self._root_path = p
            return True
            
        self._root_path = None
        return False
        
    def is_valid(self) -> bool:
        return self._root_path is not None and self._root_path.exists()
        
    def get_symbols_dir(self) -> Optional[Path]:
        # PYLANCE FIX: Explicitly prove to Pylance that _root_path is a Path object here
        if self._root_path is not None and self._root_path.exists():
            sym_dir = self._root_path / "Symbols"
            sym_dir.mkdir(parents=True, exist_ok=True)
            return sym_dir
        return None

    def clear(self):
        """Empties the in-memory cache."""
        self.categories.clear()

    def get_all_symbols(self) -> List[Symbol]:
        """Returns a flat list of every symbol in the entire library."""
        all_syms = []
        for cat in self.categories.values():
            all_syms.extend(cat.symbols)
        return all_syms


# ==========================================
# UI THEMES & STYLING
# ==========================================

class AppTheme:
    """Centralized Theme configurations so our view controllers remain clean."""
    
    @staticmethod
    def get_palette(theme_name: str, base_palette):
        """Returns a modified QPalette based on the requested theme."""
        from PySide6.QtGui import QPalette, QColor
        from PySide6.QtCore import Qt
        
        p = QPalette(base_palette)
        
        if theme_name == "Modern Dark":
            p.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
            p.setColor(QPalette.ColorRole.WindowText, QColor(224, 224, 224))
            p.setColor(QPalette.ColorRole.Base, QColor(37, 37, 38))
            p.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 48))
            p.setColor(QPalette.ColorRole.ToolTipBase, QColor(0, 122, 204))
            p.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
            p.setColor(QPalette.ColorRole.Text, QColor(224, 224, 224))
            p.setColor(QPalette.ColorRole.Button, QColor(62, 62, 66))
            p.setColor(QPalette.ColorRole.ButtonText, QColor(224, 224, 224))
            p.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
            p.setColor(QPalette.ColorRole.Link, QColor(0, 122, 204))
            p.setColor(QPalette.ColorRole.Highlight, QColor(0, 122, 204))
            p.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
        elif theme_name == "Modern Light":
            p.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
            p.setColor(QPalette.ColorRole.WindowText, QColor(33, 37, 41))
            p.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
            p.setColor(QPalette.ColorRole.AlternateBase, QColor(248, 249, 250))
            p.setColor(QPalette.ColorRole.ToolTipBase, QColor(0, 122, 204))
            p.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
            p.setColor(QPalette.ColorRole.Text, QColor(33, 37, 41))
            p.setColor(QPalette.ColorRole.Button, QColor(233, 236, 239))
            p.setColor(QPalette.ColorRole.ButtonText, QColor(33, 37, 41))
            p.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
            p.setColor(QPalette.ColorRole.Link, QColor(0, 122, 204))
            p.setColor(QPalette.ColorRole.Highlight, QColor(0, 122, 204))
            p.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
            
        return p

    @staticmethod
    def get_stylesheet(theme_name: str) -> str:
        """Returns the CSS string for the entire application."""
        if theme_name == "Modern Dark":
            return """
            QMainWindow, QDialog { background-color: #1E1E1E; color: #E0E0E0; }
            QLabel { color: #E0E0E0; }
            QToolTip { color: #ffffff; background-color: #007ACC; border: 1px solid #3E3E42; }
            QLineEdit, QComboBox, QSpinBox { padding: 5px; border: 1px solid #3E3E42; border-radius: 4px; background-color: #2D2D30; color: #E0E0E0; }
            QTextEdit, QTextBrowser { border: 1px solid #3E3E42; border-radius: 4px; background-color: #1E1E1E; color: #E0E0E0; }
            QTableWidget { background-color: #1E1E1E; color: #E0E0E0; gridline-color: #3E3E42; border: 1px solid #3E3E42; }
            QHeaderView::section { background-color: #2D2D30; color: #E0E0E0; padding: 4px; border: 1px solid #3E3E42; }
            QListWidget { background-color: #1E1E1E; border: 1px solid #3E3E42; color: #E0E0E0; }
            QListWidget::item:selected { background-color: #007ACC; color: white; }
            QTabWidget::pane { border: 1px solid #3E3E42; background: #1E1E1E; }
            QTabBar::tab { background: #2D2D30; color: #E0E0E0; padding: 8px 16px; border: 1px solid #3E3E42; }
            QTabBar::tab:selected { background: #1E1E1E; border-bottom-color: #1E1E1E; font-weight: bold; }
            
            QPushButton { border-radius: 4px; padding: 6px 12px; background-color: #3E3E42; color: #E0E0E0; border: 1px solid #555555; }
            QPushButton:hover { background-color: #4E4E52; }
            QPushButton[action="primary"] { background-color: #007ACC; color: white; font-weight: bold; border: none; }
            QPushButton[action="primary"]:hover { background-color: #0098FF; }
            QPushButton[action="primary"]:disabled { background-color: #2D2D30; color: #666666; }
            QPushButton[action="success"] { background-color: #0F9D58; color: white; font-weight: bold; border: none; }
            QPushButton[action="success"]:hover { background-color: #15B86C; }
            QPushButton[action="danger"] { color: #F14C4C; font-weight: bold; background-color: transparent; border: 1px solid #F14C4C; }
            QPushButton[action="danger"]:hover { background-color: #F14C4C; color: white; }
            QPushButton[action="link"] { background-color: transparent; border: none; color: #007ACC; font-weight: bold; text-align: left; }
            
            LoadingScreen { background-color: #252526; border-radius: 15px; margin: 20px; }
            QProgressBar { border: 2px solid #3E3E42; border-radius: 5px; background-color: #1E1E1E; text-align: center; color: transparent; } 
            QProgressBar::chunk { background-color: #007ACC; border-radius: 3px; }
            
            QStatusBar { background-color: #1E1E1E; border-top: 1px solid #3E3E42; }
            QStatusBar QLabel { color: #858585; }
            QStatusBar QProgressBar { border: 1px solid #3E3E42; border-radius: 3px; background-color: #2D2D30; text-align: center; color: transparent; }
            QStatusBar QProgressBar::chunk { background-color: #007ACC; border-radius: 2px; }
            
            MainDropZone { background-color: #252526; border: 3px dashed #454545; border-radius: 15px; margin: 20px; }
            MainDropZone[dragActive="true"] { background-color: #1C2A3A; border-color: #007ACC; }
            
            MiniDropZone { background-color: #2D2D30; border: 1px dashed #555; color: #E0E0E0; border-radius: 6px; padding: 10px; }
            MiniDropZone[state="success"] { background-color: #1B2A22; border: 1px solid #0F9D58; color: #0F9D58; }
            MiniDropZone[state="error"] { background-color: #3A1C1C; border: 1px solid #F14C4C; color: #F14C4C; }
            
            SelectionCell[selected="true"] { background-color: #1C2A3A; border: 2px solid #007ACC; border-radius: 6px; }
            SelectionCell[selected="false"] { background-color: #252526; border: 2px solid #3E3E42; border-radius: 6px; }
            
            QLineEdit[error="true"] { border: 2px solid #F14C4C; }
            
            QLabel[cssClass="header_lbl"] { background-color: #2D2D30; padding: 10px; border: 1px solid #3E3E42; border-radius: 6px; color: #E0E0E0; }
            QLabel[cssClass="huge_icon"] { font-size: 64px; background: transparent; border: none; }
            QLabel[cssClass="title_text"] { font-size: 24px; font-weight: bold; background: transparent; border: none; }
            QLabel[cssClass="muted"] { color: #858585; font-size: 14px; background: transparent; border: none;}
            QLabel[cssClass="danger_text"] { color: #F14C4C; font-weight: bold; }
            QLabel[cssClass="success_text"] { color: #0F9D58; font-weight: bold; }
            QLabel[cssClass="primary_text"] { color: #007ACC; font-weight: bold; }
            QCheckBox[cssClass="danger_text"] { color: #F14C4C; font-weight: bold; }
            QTextEdit[cssClass="warning_box"] { background-color: #332B00; color: #E0E0E0; border: 1px solid #665500; border-radius: 4px; }
            """
        elif theme_name == "Modern Light":
            return """
            QMainWindow, QDialog, QMessageBox { background-color: #F8F9FA; color: #212529; }
            QLabel { color: #212529; }
            QToolTip { color: #ffffff; background-color: #007ACC; border: 1px solid #007ACC; }
            QLineEdit, QComboBox, QSpinBox { padding: 5px; border: 1px solid #CED4DA; border-radius: 4px; background-color: #FFFFFF; color: #212529; }
            QTextEdit, QTextBrowser { border: 1px solid #CED4DA; border-radius: 4px; background-color: #FFFFFF; color: #212529; }
            QTableWidget { background-color: #FFFFFF; color: #212529; gridline-color: #CED4DA; border: 1px solid #CED4DA; }
            QHeaderView::section { background-color: #E9ECEF; color: #212529; padding: 4px; border: 1px solid #CED4DA; }
            QListWidget { background-color: #FFFFFF; border: 1px solid #CED4DA; color: #212529; }
            QListWidget::item:selected { background-color: #007ACC; color: white; }
            QTabWidget::pane { border: 1px solid #CED4DA; background: #FFFFFF; }
            QTabBar::tab { background: #E9ECEF; color: #212529; padding: 8px 16px; border: 1px solid #CED4DA; }
            QTabBar::tab:selected { background: #FFFFFF; border-bottom-color: #FFFFFF; font-weight: bold; }
            
            QPushButton { border-radius: 4px; padding: 6px 12px; background-color: #E9ECEF; color: #212529; border: 1px solid #CED4DA; }
            QPushButton:hover { background-color: #D3D9DF; }
            QPushButton[action="primary"] { background-color: #007ACC; color: white; font-weight: bold; border: none; }
            QPushButton[action="primary"]:hover { background-color: #0062A3; }
            QPushButton[action="primary"]:disabled { background-color: #E9ECEF; color: #6C757D; }
            QPushButton[action="success"] { background-color: #0F9D58; color: white; font-weight: bold; border: none; }
            QPushButton[action="success"]:hover { background-color: #0B8043; }
            QPushButton[action="danger"] { color: #F14C4C; font-weight: bold; background-color: transparent; border: 1px solid #F14C4C; }
            QPushButton[action="danger"]:hover { background-color: #F14C4C; color: white; }
            QPushButton[action="link"] { background-color: transparent; border: none; color: #007ACC; font-weight: bold; text-align: left; }
            
            LoadingScreen { background-color: #FFFFFF; border-radius: 15px; margin: 20px; border: 1px solid #CED4DA; }
            QProgressBar { border: 2px solid #CED4DA; border-radius: 5px; background-color: #E9ECEF; text-align: center; color: transparent; } 
            QProgressBar::chunk { background-color: #007ACC; border-radius: 3px; }
            
            QStatusBar { background-color: #F8F9FA; border-top: 1px solid #CED4DA; }
            QStatusBar QLabel { color: #6C757D; }
            QStatusBar QProgressBar { border: 1px solid #CED4DA; border-radius: 3px; background-color: #FFFFFF; text-align: center; color: transparent; }
            QStatusBar QProgressBar::chunk { background-color: #007ACC; border-radius: 2px; }
            
            MainDropZone { background-color: #FFFFFF; border: 3px dashed #CED4DA; border-radius: 15px; margin: 20px; }
            MainDropZone[dragActive="true"] { background-color: #E6F2FF; border-color: #007ACC; }
            
            MiniDropZone { background-color: #F8F9FA; border: 1px dashed #ADB5BD; color: #495057; border-radius: 6px; padding: 10px; }
            MiniDropZone[state="success"] { background-color: #E8F5E9; border: 1px solid #0F9D58; color: #0F9D58; }
            MiniDropZone[state="error"] { background-color: #FDE8E8; border: 1px solid #F14C4C; color: #F14C4C; }
            
            SelectionCell[selected="true"] { background-color: #E6F2FF; border: 2px solid #007ACC; border-radius: 6px; }
            SelectionCell[selected="false"] { background-color: #FFFFFF; border: 2px solid #CED4DA; border-radius: 6px; }
            
            QLineEdit[error="true"] { border: 2px solid #F14C4C; }
            
            QLabel[cssClass="header_lbl"] { background-color: #E9ECEF; padding: 10px; border: 1px solid #CED4DA; border-radius: 6px; color: #212529; }
            QLabel[cssClass="huge_icon"] { font-size: 64px; background: transparent; border: none; }
            QLabel[cssClass="title_text"] { font-size: 24px; font-weight: bold; background: transparent; border: none; }
            QLabel[cssClass="muted"] { color: #6C757D; font-size: 14px; background: transparent; border: none; }
            QLabel[cssClass="danger_text"] { color: #F14C4C; font-weight: bold; }
            QLabel[cssClass="success_text"] { color: #0F9D58; font-weight: bold; }
            QLabel[cssClass="primary_text"] { color: #007ACC; font-weight: bold; }
            QCheckBox[cssClass="danger_text"] { color: #F14C4C; font-weight: bold; }
            QTextEdit[cssClass="warning_box"] { background-color: #FFF3CD; color: #856404; border: 1px solid #FFEEBA; border-radius: 4px; }
            """
        else:
            return """
            MainDropZone { background-color: transparent; border: 2px dashed #999; border-radius: 10px; margin: 20px; }
            MainDropZone[dragActive="true"] { background-color: #e0ffe0; border-color: #00aa00; }
            MiniDropZone { background-color: transparent; border: 1px dashed #999; border-radius: 4px; padding: 10px; }
            MiniDropZone[state="success"] { background-color: #e0ffe0; border: 1px solid #00aa00; }
            MiniDropZone[state="error"] { background-color: #ffe0e0; border: 1px solid #aa0000; }
            SelectionCell[selected="true"] { background-color: #e0f0ff; border: 2px solid #0000aa; border-radius: 4px; }
            SelectionCell[selected="false"] { background-color: transparent; border: 2px solid #ccc; border-radius: 4px; }
            QLineEdit[error="true"] { border: 2px solid red; }
            QLabel[cssClass="header_lbl"] { background-color: transparent; padding: 10px; border: 1px solid #999; border-radius: 4px; color: black; }
            QLabel[cssClass="huge_icon"] { font-size: 64px; }
            QLabel[cssClass="title_text"] { font-size: 24px; font-weight: bold; }
            QLabel[cssClass="muted"] { color: #555; font-size: 14px; }
            QLabel[cssClass="danger_text"] { color: red; font-weight: bold; }
            QLabel[cssClass="success_text"] { color: green; font-weight: bold; }
            QLabel[cssClass="primary_text"] { color: blue; font-weight: bold; }
            QCheckBox[cssClass="danger_text"] { color: red; font-weight: bold; }
            QTextEdit[cssClass="warning_box"] { background-color: #ffffe0; color: #880000; border: 1px solid #aaaa00; border-radius: 4px; }
            """
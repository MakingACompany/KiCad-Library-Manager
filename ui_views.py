"""
ui_views.py
Contains all popup dialogs, drag-and-drop zones, and custom UI widgets.
Acts strictly as the "View" layer in the MVC architecture.
"""
import os
import json
import uuid
import math
import datetime
import logging
import urllib.request
import filecmp
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any

from PySide6.QtWidgets import (QLabel, QLineEdit, QComboBox, QPushButton, 
                             QDialog, QFormLayout, QVBoxLayout, QHBoxLayout,
                             QFrame, QMessageBox, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QAbstractItemView, QTextEdit, QWidget, QFileDialog, 
                             QGridLayout, QListWidget, QListWidgetItem, QCheckBox, QProgressBar,
                             QTabWidget, QInputDialog, QSpinBox, QScrollArea, QApplication,
                             QTreeWidget, QTreeWidgetItem, QTextBrowser, QGraphicsView, QGraphicsScene,
                             QSplitter, QMenu)
from PySide6.QtCore import Qt, QUrl, QSettings, Signal, QThread, QTimer, QPoint
from PySide6.QtGui import QDesktopServices, QPixmap, QImage, QFont, QPen, QColor, QBrush, QPainter, QPainterPath

from constants import DEFAULT_CATEGORIES, DEFAULT_SUBCATEGORIES, PART_FIELDS, MANUAL_HTML
from models import Symbol, Category, Pin
from file_io import FileImporter
from api import DigiKeyAPI

logger = logging.getLogger(__name__)

# ==========================================
# HELPER METHODS
# ==========================================

def get_setting_str(settings: QSettings, key: str, default: str = "") -> str:
    val = settings.value(key, default)
    return str(val) if val is not None else default

def get_setting_int(settings: QSettings, key: str, default: int) -> int:
    val = settings.value(key, default)
    if val is None: return default
    try: return int(str(val))
    except (ValueError, TypeError): return default

def get_setting_bool(settings: QSettings, key: str, default: bool) -> bool:
    val = settings.value(key, default)
    if isinstance(val, bool): return val
    return str(val).lower() == 'true'

def get_library_prefix(settings: QSettings) -> str:
    return get_setting_str(settings, "library_prefix", "Custom_")

def get_active_categories(settings: QSettings) -> dict:
    custom_cats = settings.value("custom_categories")
    if custom_cats:
        try: return json.loads(str(custom_cats))
        except json.JSONDecodeError: return DEFAULT_CATEGORIES
    return DEFAULT_CATEGORIES

def get_active_subcategories(settings: QSettings) -> dict:
    custom_subcats = settings.value("custom_subcategories")
    if custom_subcats:
        try: return json.loads(str(custom_subcats))
        except json.JSONDecodeError: return DEFAULT_SUBCATEGORIES
    return DEFAULT_SUBCATEGORIES

def sync_vendor_part_numbers(properties_dict: dict) -> dict:
    supplier = properties_dict.get("Supplier", "").strip().lower()
    supplier_part = properties_dict.get("Supplier Part", "").strip()
    if supplier and supplier_part:
        if "digikey" in supplier or "digi-key" in supplier:
            if not properties_dict.get("DigiKey_PN"): properties_dict["DigiKey_PN"] = supplier_part
        elif "lcsc" in supplier:
            if not properties_dict.get("LCSC_PN"): properties_dict["LCSC_PN"] = supplier_part
        elif "mouser" in supplier:
            if not properties_dict.get("Mouser_PN"): properties_dict["Mouser_PN"] = supplier_part
    return properties_dict

def style_combobox_dropdown(combo: QComboBox):
    """Fixes missing hover highlights in combo box dropdown lists across different OS themes."""
    if combo and combo.view():
        combo.view().setStyleSheet("""
            QAbstractItemView { 
                selection-background-color: #007ACC; 
                selection-color: white; 
            }
            QAbstractItemView::item:hover { 
                background-color: #007ACC; 
                color: white; 
            }
        """)

# ==========================================
# HARDWARE-ACCELERATED KICAD GRAPHICS ENGINE
# ==========================================

class InteractiveGraphicsView(QGraphicsView):
    def __init__(self, scene: QGraphicsScene):
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._is_panning = False
        self._pan_start = QPoint()

    def wheelEvent(self, event):
        zoom_in_factor = 1.25
        zoom_out_factor = 1 / zoom_in_factor
        if event.angleDelta().y() > 0: zoom_factor = zoom_in_factor
        else: zoom_factor = zoom_out_factor
        self.scale(zoom_factor, zoom_factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_panning:
            current_pos = event.position().toPoint()
            delta = current_pos - self._pan_start
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            if h_bar and v_bar:
                h_bar.setValue(h_bar.value() - delta.x())
                v_bar.setValue(v_bar.value() - delta.y())
            self._pan_start = current_pos
            event.accept()
        else:
            super().mouseMoveEvent(event)

class KiCadViewerWidget(QFrame):
    preview_closed = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("background-color: #f0f0f0;")
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        tb_layout = QHBoxLayout()
        tb_layout.setContentsMargins(10, 10, 10, 10)
        self.title_lbl = QLabel("<b>Blueprint Viewer</b>")
        self.title_lbl.setStyleSheet("color: black;")
        tb_layout.addWidget(self.title_lbl)
        
        self.legend_lbl = QLabel("<span style='color:green'>■ New Part</span> &nbsp;&nbsp;<span style='color:red'>■ Existing Part</span>")
        self.legend_lbl.setVisible(False)
        tb_layout.addWidget(self.legend_lbl)
        
        tb_layout.addStretch()
        
        btn_fit = QPushButton("Fit View")
        btn_fit.clicked.connect(self.fit_view)
        tb_layout.addWidget(btn_fit)
        
        self.btn_close = QPushButton("✖ Close Preview")
        self.btn_close.clicked.connect(self.preview_closed.emit)
        tb_layout.addWidget(self.btn_close)
        
        top_bar = QWidget()
        top_bar.setStyleSheet("background-color: #e0e0e0; border-bottom: 1px solid #ccc;")
        top_bar.setLayout(tb_layout)
        self.main_layout.addWidget(top_bar)

        self.scene = QGraphicsScene()
        self.view = InteractiveGraphicsView(self.scene)
        self.view.setStyleSheet("border: none; background: transparent;")
        self.main_layout.addWidget(self.view)

    def clear(self):
        self.scene.clear()
        self.legend_lbl.setVisible(False)

    def fit_view(self):
        rect = self.scene.itemsBoundingRect()
        if not rect.isNull():
            self.view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            self.view.scale(0.85, 0.85) 

    def draw_symbol(self, raw_block: str, theme: str = "standard", title="Symbol Preview"):
        self.title_lbl.setText(f"<b>{title}</b>")
        
        if theme == "standard":
            body_pen = QPen(QColor(132, 0, 0), 0.15) 
            fill_brush = QBrush(QColor(255, 255, 194, 255))
            pin_pen = QPen(QColor(132, 0, 0), 0.15)
            text_color = QColor(0, 132, 132) 
        elif theme == "new":
            body_pen = QPen(QColor(0, 157, 88, 200), 0.25)
            fill_brush = QBrush(QColor(0, 157, 88, 30))
            pin_pen = QPen(QColor(0, 157, 88, 200), 0.25)
            text_color = QColor(0, 157, 88, 200)
        elif theme == "existing":
            body_pen = QPen(QColor(241, 76, 76, 200), 0.25)
            fill_brush = QBrush(QColor(241, 76, 76, 30))
            pin_pen = QPen(QColor(241, 76, 76, 200), 0.25)
            text_color = QColor(241, 76, 76, 200)
        else:
            body_pen = QPen(QColor(132, 0, 0), 0.15)
            fill_brush = QBrush(QColor(255, 255, 194, 255))
            pin_pen = QPen(QColor(132, 0, 0), 0.15)
            text_color = QColor(0, 132, 132)
            
        import re
        
        # Filter out DeMorgan/Alternate converted blocks (ending with _2) to prevent overlapping graphics
        valid_blocks = []
        for i, sym_block in enumerate(raw_block.split('(symbol "')):
            if i > 0 and re.match(r'^[^"]*_[0-9]+_2"', sym_block):
                continue
            valid_blocks.append(sym_block)
        raw_block = '(symbol "'.join(valid_blocks)
        
        rect_pattern = r'\(rectangle\s+\(start\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)[^\)]*?\(fill\s+\(type\s+([^\)]+)\)\)'
        for m in re.finditer(rect_pattern, raw_block):
            x1, y1 = float(m.group(1)), -float(m.group(2))
            x2, y2 = float(m.group(3)), -float(m.group(4))
            fill_type = m.group(5)
            rx, ry = min(x1, x2), min(y1, y2)
            rw, rh = abs(x2 - x1), abs(y2 - y1)
            active_brush = fill_brush if fill_type == "background" else QBrush(Qt.GlobalColor.transparent)
            self.scene.addRect(rx, ry, rw, rh, body_pen, active_brush)

        rect_pattern2 = r'\(rectangle\s+\(start\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)'
        for m in re.finditer(rect_pattern2, raw_block):
            if "(fill" not in m.group(0):
                x1, y1 = float(m.group(1)), -float(m.group(2))
                x2, y2 = float(m.group(3)), -float(m.group(4))
                rx, ry = min(x1, x2), min(y1, y2)
                rw, rh = abs(x2 - x1), abs(y2 - y1)
                self.scene.addRect(rx, ry, rw, rh, body_pen, QBrush(Qt.GlobalColor.transparent))

        polygon_pattern = r'\(polygon\s+\(pts\s*((?:\(xy\s+[\-\d.]+\s+[\-\d.]+\)\s*)+)\)[^\)]*?\(fill\s+\(type\s+([^\)]+)\)\)'
        for m in re.finditer(polygon_pattern, raw_block):
            pts_str = m.group(1)
            fill_type = m.group(2)
            pts = re.findall(r'\(xy\s+([\-\d.]+)\s+([\-\d.]+)\)', pts_str)
            if len(pts) >= 3:
                path = QPainterPath()
                path.moveTo(float(pts[0][0]), -float(pts[0][1]))
                for pt in pts[1:]:
                    path.lineTo(float(pt[0]), -float(pt[1]))
                path.closeSubpath()
                active_brush = fill_brush if fill_type == "background" else QBrush(Qt.GlobalColor.transparent)
                if fill_type == "outline": active_brush = QBrush(body_pen.color())
                self.scene.addPath(path, body_pen, active_brush)

        poly_pattern = r'\(polyline\s+\(pts\s*((?:\(xy\s+[\-\d.]+\s+[\-\d.]+\)\s*)+)\)'
        for m in re.finditer(poly_pattern, raw_block):
            pts_str = m.group(1)
            pts = re.findall(r'\(xy\s+([\-\d.]+)\s+([\-\d.]+)\)', pts_str)
            if len(pts) >= 2:
                for i in range(len(pts)-1):
                    x1, y1 = float(pts[i][0]), -float(pts[i][1])
                    x2, y2 = float(pts[i+1][0]), -float(pts[i+1][1])
                    self.scene.addLine(x1, y1, x2, y2, body_pen)

        circle_pattern = r'\(circle\s+\(center\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(radius\s+([\-\d.]+)\)'
        for m in re.finditer(circle_pattern, raw_block):
            cx, cy, r = float(m.group(1)), -float(m.group(2)), float(m.group(3))
            self.scene.addEllipse(cx-r, cy-r, r*2, r*2, body_pen, fill_brush)

        arc_pattern = r'\(arc\s+\(start\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(mid\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)'
        for m in re.finditer(arc_pattern, raw_block):
            x1, y1 = float(m.group(1)), -float(m.group(2))
            x2, y2 = float(m.group(3)), -float(m.group(4))
            x3, y3 = float(m.group(5)), -float(m.group(6))
            
            D = 2 * (x1*(y2 - y3) + x2*(y3 - y1) + x3*(y1 - y2))
            if D != 0:
                xc = ((x1*x1 + y1*y1)*(y2 - y3) + (x2*x2 + y2*y2)*(y3 - y1) + (x3*x3 + y3*y3)*(y1 - y2)) / D
                yc = ((x1*x1 + y1*y1)*(x3 - x2) + (x2*x2 + y2*y2)*(x1 - x3) + (x3*x3 + y3*y3)*(x2 - x1)) / D
                r = math.hypot(x1 - xc, y1 - yc)
                
                a1 = math.atan2(y1 - yc, x1 - xc)
                a2 = math.atan2(y2 - yc, x2 - xc)
                a3 = math.atan2(y3 - yc, x3 - xc)
                
                while a2 < a1: a2 += 2*math.pi
                while a3 < a2: a3 += 2*math.pi
                if a3 - a1 > 2*math.pi:
                    a1 = math.atan2(y1 - yc, x1 - xc)
                    a2 = math.atan2(y2 - yc, x2 - xc)
                    a3 = math.atan2(y3 - yc, x3 - xc)
                    while a2 > a1: a2 -= 2*math.pi
                    while a3 > a2: a3 -= 2*math.pi
                
                path = QPainterPath()
                path.moveTo(x1, y1)
                steps = 16
                for i in range(1, steps + 1):
                    ang = a1 + i * (a3 - a1) / steps
                    path.lineTo(xc + r * math.cos(ang), yc + r * math.sin(ang))
                self.scene.addPath(path, body_pen)
            else:
                self.scene.addLine(x1, y1, x3, y3, body_pen)

        pin_blocks = raw_block.split('(pin ')
        if len(pin_blocks) > 1:
            for pb in pin_blocks[1:]:
                at_m = re.search(r'\(at\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)\)', pb)
                len_m = re.search(r'\(length\s+([\-\d.]+)\)', pb)
                name_m = re.search(r'\(name\s+"([^"]*)"', pb)
                if not name_m: name_m = re.search(r'\(name\s+(~)', pb)
                num_m = re.search(r'\(number\s+"([^"]*)"', pb)
                if not num_m: num_m = re.search(r'\(number\s+(~)', pb)

                if not at_m or not len_m: continue

                x, y, angle = map(float, at_m.groups())
                y = -y 
                length = float(len_m.group(1))

                name = name_m.group(1) if name_m else ""
                num = num_m.group(1) if num_m else ""

                junction_radius = 0.25
                self.scene.addEllipse(x-junction_radius, y-junction_radius, junction_radius*2, junction_radius*2, 
                                      QPen(Qt.GlobalColor.transparent), QBrush(pin_pen.color()))
                
                end_x, end_y = x, y
                if angle == 0: end_x += length
                elif angle == 90: end_y -= length
                elif angle == 180: end_x -= length
                elif angle == 270: end_y += length
                
                self.scene.addLine(x, y, end_x, end_y, pin_pen)
                
                font_name = QFont("Arial", 10)
                font_num = QFont("Arial", 10)

                if num and num != "~":
                    t_num = self.scene.addSimpleText(num, font_num)
                    t_num.setBrush(QBrush(text_color))
                    t_num.setScale(0.12)
                    tw = t_num.boundingRect().width() * 0.12
                    th = t_num.boundingRect().height() * 0.12
                    if angle == 0: t_num.setPos(x + length/2 - tw/2, y - th - 0.2)
                    elif angle == 180: t_num.setPos(x - length/2 - tw/2, y - th - 0.2)
                    elif angle == 90: t_num.setPos(x - tw - 0.3, y - length/2 - th/2)
                    elif angle == 270: t_num.setPos(x - tw - 0.3, y + length/2 - th/2)

                if name and name != "~":
                    t_name = self.scene.addSimpleText(name, font_name)
                    t_name.setBrush(QBrush(text_color))
                    t_name.setScale(0.12)
                    tw = t_name.boundingRect().width() * 0.12
                    th = t_name.boundingRect().height() * 0.12
                    if angle == 0: t_name.setPos(end_x + 0.5, end_y - th/2)
                    elif angle == 180: t_name.setPos(end_x - tw - 0.5, end_y - th/2)
                    elif angle == 90: t_name.setPos(end_x - tw/2, end_y - th - 0.5)
                    elif angle == 270: t_name.setPos(end_x - tw/2, end_y + 0.5)

        prop_pattern = r'\(property\s+"(Reference|Value)"\s+"([^"]+)"[^\)]*?\(at\s+([\-\d.]+)\s+([\-\d.]+)'
        for m in re.finditer(prop_pattern, raw_block):
            prop_type, prop_val, x_str, y_str = m.groups()
            x, y = float(x_str), -float(y_str)
            
            t_color = QColor(0, 132, 132) if theme == "standard" else text_color
            t = self.scene.addSimpleText(prop_val, QFont("Arial", 10, QFont.Weight.Bold))
            t.setBrush(QBrush(t_color))
            t.setScale(0.15)
            
            th = t.boundingRect().height() * 0.15
            t.setPos(x, y - th)

    def draw_footprint(self, filepath: str, theme: str = "standard", title="Footprint Preview"):
        self.title_lbl.setText(f"<b>{title}</b>")
        if not Path(filepath).exists(): return
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        if theme == "standard":
            pad_pen = QPen(QColor(132, 0, 0), 0.05)
            pad_brush = QBrush(QColor(180, 50, 50, 150))  
            text_color = QColor(0, 132, 132)
        elif theme == "new":
            pad_pen = QPen(QColor(0, 157, 88, 200), 0.1)
            pad_brush = QBrush(QColor(0, 157, 88, 50))
            text_color = QColor(0, 157, 88, 200)
        elif theme == "existing":
            pad_pen = QPen(QColor(241, 76, 76, 200), 0.1)
            pad_brush = QBrush(QColor(241, 76, 76, 50))
            text_color = QColor(241, 76, 76, 200)
        else:
            pad_pen = QPen(QColor(132, 0, 0), 0.05)
            pad_brush = QBrush(QColor(180, 50, 50, 150))
            text_color = QColor(0, 132, 132)

        def get_layer_pen(layer_name):
            if theme != "standard":
                return QPen(text_color, 0.15)
            
            # Vivid Multi-Layer Colors for an authentic KiCad appearance
            if "SilkS" in layer_name:
                c = QColor(0, 132, 132) if "F." in layer_name else QColor(132, 0, 132)
            elif "Fab" in layer_name:
                c = QColor(132, 132, 132) if "F." in layer_name else QColor(140, 100, 120)
            elif "CrtYd" in layer_name:
                c = QColor(180, 180, 180)
            elif "Edge.Cuts" in layer_name:
                c = QColor(200, 200, 0)
            else:
                c = QColor(100, 100, 100)
            return QPen(c, 0.15)

        text_font = QFont("Arial", 1)

        import re
        
        # 1. Pads & Drills (Supports Mechanical NPTH holes effortlessly!)
        pad_pattern = r'\(pad\s+"?([^"\s]*)"?\s+([a-zA-Z_]+)\s+([a-zA-Z_]+)\s+\(at\s+([\-\d.]+)\s+([\-\d.]+)[^\)]*\)\s+\(size\s+([\-\d.]+)\s+([\-\d.]+)\)'
        for m in re.finditer(pad_pattern, content):
            num, pad_type, shape, x, y, w, h = m.group(1), m.group(2), m.group(3), float(m.group(4)), float(m.group(5)), float(m.group(6)), float(m.group(7))
            
            rest_of_pad = content[m.end():m.end()+150]
            drill_match = re.search(r'\(drill\s+(?:oval\s+)?([\-\d.]+)(?:\s+([\-\d.]+))?\)', rest_of_pad)
            drill_w, drill_h = 0, 0
            if drill_match:
                drill_w = float(drill_match.group(1))
                drill_h = float(drill_match.group(2)) if drill_match.group(2) else drill_w

            # NPTH Rendering: Light mechanical grey for the pad surface
            if theme == "standard" and pad_type == 'npth':
                active_brush = QBrush(QColor(220, 220, 220, 150))
                active_pen = QPen(QColor(150, 150, 150), 0.05)
            else:
                active_brush = pad_brush
                active_pen = pad_pen
            
            # Copper / Pad Outline Area
            if shape in ['circle', 'oval']:
                self.scene.addEllipse(x - w/2, y - h/2, w, h, active_pen, active_brush)
            else:
                self.scene.addRect(x - w/2, y - h/2, w, h, active_pen, active_brush)
                
            # Drill Hole (Punched cleanly in the middle of any geometry)
            if drill_w > 0:
                d_brush = QBrush(QColor(30, 30, 30))
                d_pen = QPen(QColor(0, 0, 0), 0.05)
                self.scene.addEllipse(x - drill_w/2, y - drill_h/2, drill_w, drill_h, d_pen, d_brush)
                
            if num and num != "~":
                t = self.scene.addText(num, text_font)
                t.setDefaultTextColor(text_color)
                t_width = t.boundingRect().width()
                t_height = t.boundingRect().height()
                t.setPos(x - t_width/2, y - t_height/2)

        # 2. Geometry Layers (Lines, Arcs, Polygons natively filtered by physical layer)
        blocks = content.split('(fp_')
        for b in blocks[1:]: 
            layer_match = re.search(r'\(layer\s+"([^"]+)"\)', b)
            layer_name = layer_match.group(1) if layer_match else "F.SilkS"
            pen = get_layer_pen(layer_name)

            if b.startswith('line'):
                m = re.search(r'\(start\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)', b)
                if m:
                    self.scene.addLine(float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)), pen)
            
            elif b.startswith('rect'):
                m = re.search(r'\(start\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)', b)
                if m:
                    x1, y1, x2, y2 = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
                    rx, ry = min(x1, x2), min(y1, y2)
                    rw, rh = abs(x2 - x1), abs(y2 - y1)
                    self.scene.addRect(rx, ry, rw, rh, pen, QBrush(Qt.GlobalColor.transparent))
            
            elif b.startswith('circle'):
                m = re.search(r'\(center\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)', b)
                if m:
                    cx, cy, ex, ey = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
                    r = math.hypot(ex-cx, ey-cy)
                    self.scene.addEllipse(cx-r, cy-r, r*2, r*2, pen)
            
            elif b.startswith('arc'):
                m = re.search(r'\(start\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(mid\s+([\-\d.]+)\s+([\-\d.]+)\)\s+\(end\s+([\-\d.]+)\s+([\-\d.]+)\)', b)
                if m:
                    x1, y1 = float(m.group(1)), float(m.group(2))
                    x2, y2 = float(m.group(3)), float(m.group(4))
                    x3, y3 = float(m.group(5)), float(m.group(6))
                    
                    D = 2 * (x1*(y2 - y3) + x2*(y3 - y1) + x3*(y1 - y2))
                    if D != 0:
                        xc = ((x1*x1 + y1*y1)*(y2 - y3) + (x2*x2 + y2*y2)*(y3 - y1) + (x3*x3 + y3*y3)*(y1 - y2)) / D
                        yc = ((x1*x1 + y1*y1)*(x3 - x2) + (x2*x2 + y2*y2)*(x1 - x3) + (x3*x3 + y3*y3)*(x2 - x1)) / D
                        r = math.hypot(x1 - xc, y1 - yc)
                        
                        a1 = math.atan2(y1 - yc, x1 - xc)
                        a2 = math.atan2(y2 - yc, x2 - xc)
                        a3 = math.atan2(y3 - yc, x3 - xc)
                        
                        while a2 < a1: a2 += 2*math.pi
                        while a3 < a2: a3 += 2*math.pi
                        if a3 - a1 > 2*math.pi:
                            a1 = math.atan2(y1 - yc, x1 - xc)
                            a2 = math.atan2(y2 - yc, x2 - xc)
                            a3 = math.atan2(y3 - yc, x3 - xc)
                            while a2 > a1: a2 -= 2*math.pi
                            while a3 > a2: a3 -= 2*math.pi
                        
                        path = QPainterPath()
                        path.moveTo(x1, y1)
                        steps = 16
                        for i in range(1, steps + 1):
                            ang = a1 + i * (a3 - a1) / steps
                            path.lineTo(xc + r * math.cos(ang), yc + r * math.sin(ang))
                        self.scene.addPath(path, pen)
                    else:
                        self.scene.addLine(x1, y1, x3, y3, pen)
            
            elif b.startswith('poly'):
                m = re.search(r'\(pts\s*((?:\(xy\s+[\-\d.]+\s+[\-\d.]+\)\s*)+)\)', b)
                if m:
                    pts_str = m.group(1)
                    pts = re.findall(r'\(xy\s+([\-\d.]+)\s+([\-\d.]+)\)', pts_str)
                    if len(pts) >= 3:
                        path = QPainterPath()
                        path.moveTo(float(pts[0][0]), -float(pts[0][1]))
                        for pt in pts[1:]:
                            path.lineTo(float(pt[0]), -float(pt[1]))
                        path.closeSubpath()
                        self.scene.addPath(path, pen, QBrush(pen.color()))

    def overlay_symbols(self, block_left: str, block_right: str):
        self.clear()
        self.legend_lbl.setVisible(True)
        self.draw_symbol(block_right, theme="existing", title="Visual Conflict Comparison")
        self.draw_symbol(block_left, theme="new", title="Visual Conflict Comparison")
        self.fit_view()

# ==========================================
# CUSTOM WIDGETS
# ==========================================

class ImagePreviewDialog(QDialog):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Full Image Preview")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("background-color: #f0f0f0;")
        
        pm = QPixmap(image_path)
        max_w, max_h = 800, 600
        if not pm.isNull():
            screen = QApplication.primaryScreen()
            if screen:
                avail = screen.availableGeometry()
                max_w = int(avail.width() * 0.9)
                max_h = int(avail.height() * 0.9)
                
            if pm.width() > max_w or pm.height() > max_h:
                pm = pm.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            lbl.setPixmap(pm)
            
        scroll = QScrollArea()
        scroll.setWidget(lbl)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(scroll)
        
        w = min(pm.width() + 40, max_w) if not pm.isNull() else 400
        h = min(pm.height() + 40, max_h) if not pm.isNull() else 300
        self.resize(w, h)

class NoDeselectTreeWidget(QTreeWidget):
    """A custom TreeWidget that prevents clicking on empty space from clearing the selection."""
    def mousePressEvent(self, event):
        item = self.itemAt(event.pos())
        if item is None:
            return
        super().mousePressEvent(event)

class SelectionCell(QFrame):
    cellClicked = Signal(str) 

    def __init__(self, side: str, widget: QWidget, match_state: str = "conflict", parent=None):
        super().__init__(parent)
        self.side = side
        self.widget = widget
        self.content = ""
        self.match_state = match_state
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.addWidget(self.widget)
        self.set_selected(False)
        self._install_filters(self.widget)

    def _install_filters(self, widget: QWidget):
        if isinstance(widget, QLabel): widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        else: widget.installEventFilter(self)
        
        for child in widget.findChildren(QWidget):
            if isinstance(child, QLabel): child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            elif not isinstance(child, QPushButton): child.installEventFilter(self)

    def mousePressEvent(self, event):
        self.cellClicked.emit(self.side)
        super().mousePressEvent(event)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.MouseButtonPress:
            self.cellClicked.emit(self.side)
        return super().eventFilter(obj, event)

    def set_match_state(self, match_state: str):
        if self.match_state != match_state:
            self.match_state = match_state
            self._apply_style()

    def set_selected(self, is_selected: bool):
        self.setProperty("selected", "true" if is_selected else "false")
        self._apply_style()

    def _apply_style(self):
        if self.match_state == "match":
            self.setStyleSheet("SelectionCell { background-color: rgba(15, 157, 88, 0.15); border: 2px solid #0F9D58; border-radius: 4px; }")
        elif self.property("selected") == "true":
            self.setStyleSheet("") 
        else:
            self.setStyleSheet("")
            
        self.style().unpolish(self)
        self.style().polish(self)

class ImageDropPasteLabel(QLabel):
    imageUpdated = Signal(str)
    imageClicked = Signal(str)

    def __init__(self, tmp_dir: Optional[Path], parent=None):
        super().__init__(parent)
        self.tmp_dir = tmp_dir
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.file_path = ""
        self.clear_image()
        
    def show_context_menu(self, pos):
        menu = QMenu(self)
        paste_action = menu.addAction("Paste Image")
        
        clipboard = QApplication.clipboard()
        if not clipboard.mimeData().hasImage():
            paste_action.setEnabled(False)
            
        action = menu.exec(self.mapToGlobal(pos))
        if action == paste_action:
            self.paste_from_clipboard()

    def paste_from_clipboard(self):
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage):
                self.save_and_set_image(image)
            elif isinstance(image, QPixmap):
                self.save_and_set_image(image.toImage())
                
    def clear_image(self):
        self.file_path = ""
        self.clear()
        self.setText("Drag & Drop Image\nOR\nPaste (Ctrl+V)")
        self.setStyleSheet("border: 2px dashed #ccc; border-radius: 8px; color: #888; background: transparent; font-weight: bold;")
        self.setToolTip("Double-click to view full size")

    def dragEnterEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.accept()
            self.setStyleSheet("border: 2px dashed #0F9D58; border-radius: 8px; color: #0F9D58; background: rgba(15, 157, 88, 0.1); font-weight: bold;")
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        if not self.file_path:
            self.setStyleSheet("border: 2px dashed #ccc; border-radius: 8px; color: #888; background: transparent; font-weight: bold;")
        else:
            self.setStyleSheet("border: 1px solid #ccc; border-radius: 8px;")

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls():
            url = mime.urls()[0]
            path = url.toLocalFile()
            if Path(path).suffix.lower() in ['.png', '.jpg', '.jpeg', '.bmp', '.svg']:
                self.set_image_path(path)
                event.accept()
            else:
                self.dragLeaveEvent(event)
                event.ignore()
        elif mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage):
                self.save_and_set_image(image)
            elif isinstance(image, QPixmap):
                self.save_and_set_image(image.toImage())
            event.accept()
        else:
            self.dragLeaveEvent(event)
            event.ignore()

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_V:
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime.hasImage():
                image = mime.imageData()
                if isinstance(image, QImage):
                    self.save_and_set_image(image)
                elif isinstance(image, QPixmap):
                    self.save_and_set_image(image.toImage())
        super().keyPressEvent(event)
        
    def mousePressEvent(self, event):
        self.setFocus()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.file_path and Path(self.file_path).exists():
            self.imageClicked.emit(self.file_path)
        super().mouseDoubleClickEvent(event)

    def save_and_set_image(self, image: QImage):
        if self.tmp_dir:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            filename = f"pasted_image_{uuid.uuid4().hex[:8]}.png"
            filepath = self.tmp_dir / filename
            image.save(str(filepath))
            self.set_image_path(str(filepath))

    def set_image_path(self, path: str):
        self.file_path = path
        pm = QPixmap(path)
        if not pm.isNull():
            self.setPixmap(pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.setStyleSheet("border: 1px solid #ccc; border-radius: 8px;")
        self.imageUpdated.emit(path)

    def resizeEvent(self, event):
        if self.file_path and Path(self.file_path).exists():
            pm = QPixmap(self.file_path)
            if not pm.isNull():
                self.setPixmap(pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        super().resizeEvent(event)


class MiniDropZone(QFrame):
    fileDropped = Signal(str)
    clicked = Signal()
    cleared = Signal()
    
    def __init__(self, asset_type: str, expected_extensions: tuple, parent=None):
        super().__init__(parent)
        self.asset_type = asset_type
        self.expected_extensions = expected_extensions
        self.setAcceptDrops(True)
        self.file_path: Optional[str] = None
        self.is_library_link = False
        self.cleared_by_user = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(75)
        
        self.main_layout = QGridLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        
        exts_str = " ".join(expected_extensions)
        self.default_text = f"Drag {asset_type} here\n({exts_str})"
        
        self.label = QLabel(self.default_text)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.main_layout.addWidget(self.label, 0, 0)
        
        self.btn_clear = QPushButton("\U0001F5D1\uFE0E", self)
        self.btn_clear.setFixedSize(26, 26)
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.setToolTip(f"Remove {asset_type}")
        
        self.btn_clear.setStyleSheet("""
            QPushButton { 
                background: transparent;
                font-family: "Segoe UI Symbol", "DejaVu Sans", "Consolas", serif;
                font-size: 14px;
                color: #333333; 
                border: none;
                border-radius: 13px;
                padding: 5px;
                margin: 0px;
            }
            QPushButton:hover { 
                color: #F14C4C;
                background: rgba(241, 76, 76, 0.25);
            }
        """)
        self.btn_clear.clicked.connect(self.clear_file)
        self.btn_clear.hide()
        
        self.set_state("default")

    def enterEvent(self, event):
        if self.property("state") == "success":
            self.btn_clear.show()
            self.btn_clear.raise_()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.btn_clear.hide()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        btn_y = int((self.height() - self.btn_clear.height()) / 2)
        self.btn_clear.move(self.width() - 26, btn_y)
        self.btn_clear.raise_()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
        
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            if Path(url.toLocalFile()).suffix.lower() in self.expected_extensions:
                event.accept()
                return
        event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            self.set_file(file_path)
            self.fileDropped.emit(file_path)
            event.accept()

    def clear_file(self):
        self.cleared_by_user = True
        self.reset()
        self.cleared.emit()

    def set_file(self, filepath: str):
        self.cleared_by_user = False
        self.file_path = filepath
        name = Path(filepath).name
        fm = self.label.fontMetrics()
        elided_name = fm.elidedText(name, Qt.TextElideMode.ElideMiddle, self.width() - 35)
        self.label.setText(f"Loaded {self.asset_type}:\n{elided_name}")
        self.set_state("success")

    def reset(self):
        self.file_path = None
        self.is_library_link = False
        self.label.setText(self.default_text)
        self.set_state("default")
        
    def set_state(self, state: str):
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        
        if state == "success":
            self.label.setStyleSheet("color: #0F9D58; font-weight: bold;")
            if self.underMouse():
                self.btn_clear.show()
                self.btn_clear.raise_()
            else:
                self.btn_clear.hide()
        elif state == "error":
            self.label.setStyleSheet("color: #F14C4C; font-weight: bold;")
            self.btn_clear.hide()
        else:
            self.label.setStyleSheet("")
            self.btn_clear.hide()

class MainDropZone(QFrame):
    filesDropped = Signal(list)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setProperty("dragActive", False)
        
        main_layout = QVBoxLayout(self)
        self.icon_lbl = QLabel("📦")
        self.icon_lbl.setProperty("cssClass", "huge_icon")
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.text_lbl = QLabel("Drag and Drop")
        self.text_lbl.setProperty("cssClass", "title_text")
        self.text_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.sub_lbl = QLabel("Supports .zip, .elibz, or loose component files\n(Symbols, Footprints, Models, Datasheets, Images)")
        self.sub_lbl.setProperty("cssClass", "muted")
        self.sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub_lbl.setWordWrap(True)
        
        main_layout.addStretch()
        main_layout.addWidget(self.icon_lbl)
        main_layout.addWidget(self.text_lbl)
        main_layout.addWidget(self.sub_lbl)
        main_layout.addStretch()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.setProperty("dragActive", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.accept()
        else: event.ignore()

    def dragLeaveEvent(self, event):
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event):
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        urls = event.mimeData().urls()
        if urls:
            files = [u.toLocalFile() for u in urls]
            self.filesDropped.emit(files)

class LoadingScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        main_layout = QVBoxLayout(self)
        
        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setFixedSize(200, 15)
        
        self.lbl = QLabel("Initializing Library Engine...")
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        main_layout.addStretch()
        main_layout.addWidget(self.lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignCenter)
        main_layout.addStretch()

# ==========================================
# FULL PAGE DIALOGS
# ==========================================

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Application Settings")
        self.resize(600, 500)
        self.settings = QSettings("OpenSourceTools", "KiCadLibManager")
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        tabs = QTabWidget()
        
        general_tab = QWidget()
        g_layout = QFormLayout(general_tab)
        
        self.lib_path = QLineEdit(get_setting_str(self.settings, "library_root"))
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self.browse_root)
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.lib_path)
        path_layout.addWidget(btn_browse)
        g_layout.addRow("Library Root Folder:", path_layout)
        
        self.kicad_var_input = QLineEdit(get_setting_str(self.settings, "kicad_path_var", "${CUSTOM_LIB_DIR}"))
        self.kicad_var_input.setPlaceholderText("e.g. ${CUSTOM_LIB_DIR}")
        g_layout.addRow("KiCad Path Variable (For 3D Models):", self.kicad_var_input)
        
        self.prefix = QLineEdit(get_setting_str(self.settings, "library_prefix", "Custom_"))
        g_layout.addRow("Library Prefix:", self.prefix)
        
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Modern Light", "Modern Dark", "Classic"])
        self.theme_combo.setCurrentText(get_setting_str(self.settings, "theme", "Modern Light"))
        style_combobox_dropdown(self.theme_combo)
        g_layout.addRow("Application Theme:", self.theme_combo)
        
        self.chk_always_top = QCheckBox()
        self.chk_always_top.setChecked(get_setting_bool(self.settings, "always_on_top", False))
        g_layout.addRow("Always on Top:", self.chk_always_top)
        
        self.chk_del_orig = QCheckBox()
        self.chk_del_orig.setChecked(get_setting_bool(self.settings, "delete_originals", False))
        g_layout.addRow("Delete drag-and-dropped original files after success:", self.chk_del_orig)
        
        self.chk_sanitize = QCheckBox()
        self.chk_sanitize.setChecked(get_setting_bool(self.settings, "sanitize_names", True))
        g_layout.addRow("Auto-sanitize component names (replace spaces with underscores):", self.chk_sanitize)
        
        self.backups_spin = QSpinBox()
        self.backups_spin.setRange(0, 50)
        self.backups_spin.setValue(get_setting_int(self.settings, "backups_to_keep", 5))
        g_layout.addRow("Number of backup files (.bak) to keep per library:", self.backups_spin)
        
        self.backup_win_spin = QSpinBox()
        self.backup_win_spin.setRange(0, 60)
        self.backup_win_spin.setValue(get_setting_int(self.settings, "backup_window_minutes", 5))
        g_layout.addRow("Backup grouping window (minutes):", self.backup_win_spin)
        
        tabs.addTab(general_tab, "General")
        
        cat_tab = QWidget()
        c_layout = QVBoxLayout(cat_tab)
        c_layout.addWidget(QLabel("Define the top-level categories available for importing parts.\n(Unchecking a category does NOT delete its file)."))
        
        add_cat_layout = QHBoxLayout()
        self.new_cat_input = QLineEdit()
        self.new_cat_input.setPlaceholderText("New Category Name...")
        btn_add_cat = QPushButton("Add")
        btn_add_cat.clicked.connect(self.add_custom_category)
        add_cat_layout.addWidget(self.new_cat_input)
        add_cat_layout.addWidget(btn_add_cat)
        c_layout.addLayout(add_cat_layout)
        
        self.cat_list = QListWidget()
        
        active_cats = get_active_categories(self.settings)
        all_display_cats = set(DEFAULT_CATEGORIES.keys()).union(set(active_cats.keys()))
        
        for cat in sorted(list(all_display_cats)):
            item = QListWidgetItem(cat)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if cat in active_cats else Qt.CheckState.Unchecked)
            self.cat_list.addItem(item)
            
        c_layout.addWidget(self.cat_list)
        tabs.addTab(cat_tab, "Categories")

        api_tab = QWidget()
        a_layout = QFormLayout(api_tab)
        a_layout.addRow(QLabel("<b>Digi-Key API Configuration</b>"))
        a_layout.addRow(QLabel("<small>Required for fetching metadata and datasheets automatically.</small>"))
        
        self.dk_client_id = QLineEdit(get_setting_str(self.settings, "dk_client_id"))
        self.dk_client_id.setEchoMode(QLineEdit.EchoMode.Password)
        a_layout.addRow("Client ID:", self.dk_client_id)
        
        self.dk_client_secret = QLineEdit(get_setting_str(self.settings, "dk_client_secret"))
        self.dk_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        a_layout.addRow("Client Secret:", self.dk_client_secret)
        
        btn_test_dk = QPushButton("Test API Credentials")
        btn_test_dk.clicked.connect(self.test_dk_api)
        a_layout.addRow("", btn_test_dk)
        
        tabs.addTab(api_tab, "Integrations")

        main_layout.addWidget(tabs)
        
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("Save && Apply")
        btn_save.setProperty("action", "primary")
        btn_save.clicked.connect(self.save_settings)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        main_layout.addLayout(btn_layout)

    def browse_root(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Library Root")
        if folder: self.lib_path.setText(folder)

    def add_custom_category(self):
        new_cat = self.new_cat_input.text().strip()
        if not new_cat: return
        new_cat = FileImporter.sanitize_name(new_cat)
        
        for i in range(self.cat_list.count()):
            item = self.cat_list.item(i)
            if item is not None and item.text().lower() == new_cat.lower():
                return
                
        item = QListWidgetItem(new_cat)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        self.cat_list.addItem(item)
        self.new_cat_input.clear()

    def test_dk_api(self):
        cid = self.dk_client_id.text().strip()
        csec = self.dk_client_secret.text().strip()
        if not cid or not csec:
            QMessageBox.warning(self, "Missing Credentials", "Please enter both Client ID and Secret.")
            return
            
        api = DigiKeyAPI()
        success, msg = api.test_credentials(cid, csec)
        if success:
            QMessageBox.information(self, "Success", "Successfully authenticated with Digi-Key API!")
        else:
            QMessageBox.critical(self, "Error", f"Failed to authenticate:\n{msg}")

    def save_settings(self):
        root = self.lib_path.text().strip()
        if not root or not Path(root).exists():
            QMessageBox.warning(self, "Validation Error", "Please select a valid Library Root folder.")
            return
            
        self.settings.setValue("library_root", root)
        self.settings.setValue("library_prefix", self.prefix.text().strip())
        self.settings.setValue("kicad_path_var", self.kicad_var_input.text().strip())
        self.settings.setValue("theme", self.theme_combo.currentText())
        self.settings.setValue("always_on_top", self.chk_always_top.isChecked())
        self.settings.setValue("delete_originals", self.chk_del_orig.isChecked())
        self.settings.setValue("sanitize_names", self.chk_sanitize.isChecked())
        self.settings.setValue("backups_to_keep", self.backups_spin.value())
        self.settings.setValue("backup_window_minutes", self.backup_win_spin.value())
        self.settings.setValue("dk_client_id", self.dk_client_id.text().strip())
        self.settings.setValue("dk_client_secret", self.dk_client_secret.text().strip())
        
        new_cats = {}
        for i in range(self.cat_list.count()):
            item = self.cat_list.item(i)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                cat_name = item.text()
                new_cats[cat_name] = DEFAULT_CATEGORIES.get(cat_name, "")
        self.settings.setValue("custom_categories", json.dumps(new_cats))
        
        self.accept()


# ==========================================
# UNIFIED SYMBOL EDITOR DIALOG
# ==========================================

class SymbolEditorDialog(QDialog):
    def __init__(self, parent=None, symbol: Optional[Symbol]=None, tmp_dir: Optional[Path]=None, 
                 prefilled_assets=None, cached_names=None, is_batch=False, is_edit=False):
        super().__init__(parent)
        self.is_edit = is_edit
        self.is_batch = is_batch
        title = f"Edit Component: {symbol.name if symbol else ''}" if is_edit else ("Batch Import Component" if is_batch else "Import Component")
        self.setWindowTitle(title)
        
        self.resize(850, 920)
        
        self.symbol = symbol or Symbol("Unknown_Part", "Uncategorized")
        self.original_name = self.symbol.name
        self.tmp_dir = tmp_dir
        self.settings = QSettings("OpenSourceTools", "KiCadLibManager")
        self.cached_names = cached_names if cached_names else set()
        self.abort_batch = False
        self.nav_intent = None
        
        self.inputs: Dict[str, QWidget] = {}
        self.prefilled_assets = prefilled_assets or {}
        self.working_properties = sync_vendor_part_numbers(dict(self.symbol.properties))
        self.dk_api = DigiKeyAPI()
        
        self.save_callback: Optional[Callable] = None
        self.subcat_combo: Optional[QComboBox] = None
        
        self._init_complete = False
        self._is_dirty = False
        
        self.init_ui()
        self.populate_initial_data()

    def mark_dirty(self, *args, **kwargs):
        """Sets the dirty flag indicating the user has made an active interaction."""
        if getattr(self, '_init_complete', False):
            self._is_dirty = True

    def showEvent(self, event):
        super().showEvent(event)
        parent_w = self.parentWidget()
        if parent_w is not None:
            parent_geom = parent_w.geometry()
            my_geom = self.geometry()
            x = parent_geom.center().x() - my_geom.width() // 2
            y = parent_geom.center().y() - my_geom.height() // 2
            self.move(max(0, x), max(0, y))

    def _on_category_changed(self, text: str):
        self._update_subcategories(text)

    def _update_subcategories(self, cat_name: str, set_val: str = ""):
        combo = self.subcat_combo
        if not isinstance(combo, QComboBox): return
        
        combo.blockSignals(True)
        combo.clear()
        
        subcats = get_active_subcategories(self.settings).get(cat_name, [])
        combo.addItem("")
        combo.addItems(sorted(subcats))
        combo.insertSeparator(combo.count())
        combo.addItem("Add New Sub Category...")
        
        if set_val and set_val not in subcats and set_val != "Add New Sub Category...":
            combo.insertItem(combo.count() - 2, set_val)
            
        if set_val: combo.setCurrentText(set_val)
        else: combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def check_new_subcategory(self, text: str):
        combo = self.subcat_combo
        if not isinstance(combo, QComboBox): return
        
        if text == "Add New Sub Category...":
            new_subcat, ok = QInputDialog.getText(self, "New Sub Category", "Enter new subcategory:")
            if ok and new_subcat.strip():
                new_subcat = new_subcat.strip()
                subcats_dict = get_active_subcategories(self.settings)
                
                cat_name = self.category_combo.currentText()
                if cat_name not in subcats_dict: subcats_dict[cat_name] = []
                if new_subcat not in subcats_dict[cat_name]:
                    subcats_dict[cat_name].append(new_subcat)
                    self.settings.setValue("custom_subcategories", json.dumps(subcats_dict))
                
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("")
                combo.addItems(sorted(subcats_dict[cat_name]))
                combo.insertSeparator(combo.count())
                combo.addItem("Add New Sub Category...")
                combo.setCurrentText(new_subcat)
                combo.blockSignals(False)
            else:
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)

    def _build_fields(self, field_list, layout):
        skip_next = False
        for i, field in enumerate(field_list):
            if skip_next:
                skip_next = False
                continue

            key = field["key"]
            f_type = field.get("type", "text")
            current_val = self.working_properties.get(key, "")
            
            if f_type == "hidden": continue
            if key.lower() in ["footprint", "datasheet", "image_file", "3d_model", "3d model", "uuid", "suggested_category"]: continue
            
            label = field.get("label", key)
            
            if key == "Voltage Rating" and i + 1 < len(field_list) and field_list[i+1]["key"] == "Current Rating":
                next_field = field_list[i+1]
                next_key = next_field["key"]
                next_val = self.working_properties.get(next_key, "")

                row_layout = QHBoxLayout()
                
                w1 = QLineEdit(str(current_val))
                w1.setCursorPosition(0)
                w1.setPlaceholderText("V")
                w1.textChanged.connect(lambda t, k=key, w=w1: self.working_properties.update({k: w.text()}))
                w1.textChanged.connect(self.mark_dirty)
                self.inputs[key] = w1
                
                w2 = QLineEdit(str(next_val))
                w2.setCursorPosition(0)
                w2.setPlaceholderText("A")
                w2.textChanged.connect(lambda t, k=next_key, w=w2: self.working_properties.update({k: w.text()}))
                w2.textChanged.connect(self.mark_dirty)
                self.inputs[next_key] = w2
                
                row_layout.addWidget(w1)
                row_layout.addWidget(w2)
                
                layout.addRow("Voltage / Current Rating:", row_layout)
                skip_next = True
                continue
            
            if f_type == "subcategory_list" or key.lower() == "subcategory":
                self.subcat_combo = QComboBox()
                style_combobox_dropdown(self.subcat_combo)
                self._update_subcategories(self.category_combo.currentText(), str(current_val))
                self.subcat_combo.currentTextChanged.connect(self.check_new_subcategory)
                self.subcat_combo.currentTextChanged.connect(self.mark_dirty)
                self.inputs[key] = self.subcat_combo
                layout.addRow(label + ":", self.subcat_combo)
                
            elif f_type in ["file_footprint", "file_datasheet"]:
                row = QHBoxLayout()
                inp = QLineEdit(str(current_val))
                inp.setCursorPosition(0)
                inp.textChanged.connect(self.mark_dirty)
                self.inputs[key] = inp
                btn = QPushButton("Browse")
                btn.clicked.connect(lambda checked=False, i=inp, t=f_type: self.browse_file(i, t))
                row.addWidget(inp)
                row.addWidget(btn)
                layout.addRow(label + ":", row)
                
            elif f_type == "url":
                row = QHBoxLayout()
                inp = QLineEdit(str(current_val))
                inp.setCursorPosition(0)
                inp.textChanged.connect(self.mark_dirty)
                self.inputs[key] = inp
                btn = QPushButton("Open Link")
                btn.clicked.connect(lambda checked=False, widget=inp: QDesktopServices.openUrl(QUrl(widget.text())))
                row.addWidget(inp)
                row.addWidget(btn)
                layout.addRow(label + ":", row)
                
            elif f_type == "list":
                row = QHBoxLayout()
                inp = QLineEdit(str(current_val))
                inp.setCursorPosition(0)
                inp.textChanged.connect(self.mark_dirty)
                self.inputs[key] = inp
                btn = QPushButton("Browse...")
                btn.clicked.connect(lambda checked=False, widget=inp: self.browse_linked_parts(widget))
                row.addWidget(inp)
                row.addWidget(btn)
                layout.addRow(label + ":", row)
                
            else:
                w = QLineEdit(str(current_val))
                w.setCursorPosition(0)
                w.textChanged.connect(self.mark_dirty)
                self.inputs[key] = w
                layout.addRow(label + ":", w)

    def browse_file(self, line_edit, file_type):
        lib_root = get_setting_str(self.settings, "library_root")
        folder = "Footprints" if file_type == "file_footprint" else "Datasheets"
        start = os.path.expanduser("~")
        if lib_root:
            target_dir = Path(lib_root) / folder
            if target_dir.exists():
                start = str(target_dir)
            else:
                start = str(lib_root)
                
        filt = "Footprints (*.kicad_mod);;All (*)" if file_type == "file_footprint" else "PDF (*.pdf);;All (*)"
        path, _ = QFileDialog.getOpenFileName(self, f"Select File", start, filt)
        if path: line_edit.setText(path)
        
    def browse_drop_file(self, dropzone, filt, folder_name=""):
        lib_root = get_setting_str(self.settings, "library_root")
        start = os.path.expanduser("~")
        if lib_root:
            target_dir = Path(lib_root) / folder_name
            if target_dir.exists():
                start = str(target_dir)
            else:
                start = str(lib_root)
                
        path, _ = QFileDialog.getOpenFileName(self, f"Select File", start, filt)
        if path:
            dropzone.set_file(path)
            self.mark_dirty()
        
    def open_footprint_chooser(self):
        lib_root = get_setting_str(self.settings, "library_root")
        if not lib_root or not Path(lib_root).exists():
            QMessageBox.warning(self, "Setup Required", "Library Root is not configured.")
            return
            
        cat_name = self.category_combo.currentText().strip()
        
        current_fp = ""
        if self.footprint_drop.file_path:
            current_fp = Path(self.footprint_drop.file_path).stem
        elif self.working_properties.get("Footprint"):
            current_fp = self.working_properties["Footprint"].split(":")[-1]
        
        dialog = FootprintChooserDialog(self, Path(lib_root), cat_name, current_fp)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            fp_path = dialog.selected_filepath
            if fp_path:
                self.footprint_drop.set_file(fp_path)
                self.footprint_drop.is_library_link = True
                fp_name = Path(fp_path).stem
                self.working_properties["Footprint"] = f"{dialog.cat_combo.currentText()}:{fp_name}"
                self.mark_dirty()
        
    def browse_linked_parts(self, line_edit):
        dialog = PartSelectionDialog(self, self.cached_names)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.get_selected_parts()
            if selected:
                existing = [s.strip() for s in line_edit.text().split(',') if s.strip()]
                combined = list(set(existing + selected))
                line_edit.setText(", ".join(combined))

    def on_image_updated(self, path):
        self.working_properties["Image_File"] = path
        self.btn_clear_img.show()
        
    def clear_image(self):
        self.img_lbl.clear_image()
        self.working_properties.pop("Image_File", None)
        self.btn_clear_img.hide()

    def view_full_image(self, path):
        if path and Path(path).exists():
            dialog = ImagePreviewDialog(path, self)
            dialog.exec()

    def init_ui(self):
        main_box = QVBoxLayout(self)
        main_box.setContentsMargins(10, 10, 10, 10)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_box.addWidget(self.splitter)
        
        self.left_panel = QWidget()
        main_layout = QVBoxLayout(self.left_panel)
        main_layout.setContentsMargins(0,0,0,0)
        
        self.viewer_panel = KiCadViewerWidget()
        self.viewer_panel.setVisible(False)
        self.viewer_panel.preview_closed.connect(self.close_viewer)
        
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.viewer_panel)
        self.splitter.setStretchFactor(0, 6)
        self.splitter.setStretchFactor(1, 4)

        top_layout = QHBoxLayout()
        cat_layout = QVBoxLayout()
        cat_layout.addWidget(QLabel("<b>Category:</b>"))
        self.category_combo = QComboBox()
        style_combobox_dropdown(self.category_combo)
        active_cats = get_active_categories(self.settings)
        self.category_combo.addItems(sorted(list(active_cats.keys())))
        self.category_combo.currentTextChanged.connect(self._on_category_changed)
        self.category_combo.currentTextChanged.connect(self.mark_dirty)
        cat_layout.addWidget(self.category_combo)
        
        name_layout = QVBoxLayout()
        name_layout.addWidget(QLabel("<b>Part Name:</b>"))
        self.part_name_input = QLineEdit()
        self.part_name_input.textChanged.connect(self.check_duplicate_name)
        self.part_name_input.textChanged.connect(self.mark_dirty)
        name_layout.addWidget(self.part_name_input)
        
        top_layout.addLayout(cat_layout, 1)
        top_layout.addLayout(name_layout, 2)
        main_layout.addLayout(top_layout)
        
        self.dup_warning_lbl = QLabel("")
        self.dup_warning_lbl.setProperty("cssClass", "danger_text")
        self.dup_warning_lbl.hide()
        main_layout.addWidget(self.dup_warning_lbl)
        
        dk_layout = QHBoxLayout()
        self.dk_pn_input = QLineEdit()
        self.dk_pn_input.setPlaceholderText("Enter Digi-Key PN or Manufacturer PN...")
        self.inputs["DigiKey_PN"] = self.dk_pn_input
        self.dk_btn = QPushButton("🪄 Auto-Fill (Digi-Key)")
        self.dk_btn.clicked.connect(self.fetch_digikey_data)
        dk_layout.addWidget(QLabel("<b>Smart Fetch:</b>"))
        dk_layout.addWidget(self.dk_pn_input)
        dk_layout.addWidget(self.dk_btn)
        main_layout.addLayout(dk_layout)
        
        asset_layout = QHBoxLayout()
        self.footprint_drop = MiniDropZone("Footprint", (".kicad_mod",))
        self.model_drop = MiniDropZone("3D Model", (".step", ".stp", ".wrl"))
        self.datasheet_drop = MiniDropZone("Datasheet", (".pdf",))
        
        self.footprint_drop.clicked.connect(self.open_footprint_chooser)
        self.footprint_drop.fileDropped.connect(self.mark_dirty)
        self.footprint_drop.cleared.connect(self.mark_dirty)
        
        self.model_drop.clicked.connect(lambda: self.browse_drop_file(self.model_drop, "3D Models (*.step *.stp *.wrl)", "3D_Models"))
        self.model_drop.fileDropped.connect(self.mark_dirty)
        self.model_drop.cleared.connect(self.mark_dirty)
        
        self.datasheet_drop.clicked.connect(lambda: self.browse_drop_file(self.datasheet_drop, "Datasheets (*.pdf)", "Datasheets"))
        self.datasheet_drop.fileDropped.connect(self.mark_dirty)
        self.datasheet_drop.cleared.connect(self.mark_dirty)
        
        asset_layout.addWidget(self.footprint_drop)
        asset_layout.addWidget(self.model_drop)
        asset_layout.addWidget(self.datasheet_drop)
        main_layout.addLayout(asset_layout)

        viewer_btn_layout = QHBoxLayout()
        btn_view_sym = QPushButton("👁 View Symbol Layout")
        btn_view_sym.clicked.connect(self.view_symbol)
        btn_view_fp = QPushButton("👁 View Footprint")
        btn_view_fp.clicked.connect(self.view_footprint)
        viewer_btn_layout.addWidget(btn_view_sym)
        viewer_btn_layout.addWidget(btn_view_fp)
        main_layout.addLayout(viewer_btn_layout)

        middle_layout = QHBoxLayout()

        basic_form = QFrame()
        basic_layout = QFormLayout(basic_form)
        basic_layout.addRow(QLabel("<b>Core Properties</b>"))
        
        self.desc_input = QLineEdit()
        self.mpn_input = QLineEdit()
        self.mfg_input = QLineEdit()
        self.inputs["Description"] = self.desc_input
        self.inputs["MPN"] = self.mpn_input
        self.inputs["Manufacturer"] = self.mfg_input
        
        basic_layout.addRow("Description:", self.desc_input)
        basic_layout.addRow("MPN:", self.mpn_input)
        basic_layout.addRow("Manufacturer:", self.mfg_input)
        middle_layout.addWidget(basic_form)

        img_container = QWidget()
        img_container.setFixedWidth(160)
        img_layout = QVBoxLayout(img_container)
        img_layout.setContentsMargins(0, 0, 0, 0)
        
        self.img_lbl = ImageDropPasteLabel(self.tmp_dir)
        self.img_lbl.setFixedSize(140, 140)
        self.img_lbl.imageUpdated.connect(self.on_image_updated)
        self.img_lbl.imageUpdated.connect(self.mark_dirty)
        self.img_lbl.imageClicked.connect(self.view_full_image)
        img_layout.addWidget(self.img_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        
        self.btn_clear_img = QPushButton("Clear Image")
        self.btn_clear_img.clicked.connect(self.clear_image)
        self.btn_clear_img.clicked.connect(self.mark_dirty)
        self.btn_clear_img.hide()
        img_layout.addWidget(self.btn_clear_img)
        
        middle_layout.addWidget(img_container)
        main_layout.addLayout(middle_layout)

        self.btn_toggle_adv = QPushButton("▼ Show Advanced Properties")
        self.btn_toggle_adv.setProperty("action", "link")
        self.btn_toggle_adv.clicked.connect(self.toggle_advanced)
        main_layout.addWidget(self.btn_toggle_adv)

        self.advanced_form = QFrame()
        self.advanced_form.hide()
        adv_layout = QFormLayout(self.advanced_form)
        
        adv_fields = []
        for section_keys in PART_FIELDS.values():
            for field in section_keys:
                if field["key"] not in self.inputs:
                    adv_fields.append(field)
                    
        known_keys = {f["key"] for f in adv_fields} | set(self.inputs.keys())
        skip_keys = {"footprint", "datasheet", "image_file", "3d_model", "3d model", "uuid", "suggested_category"}
        
        extra_keys = [k for k in self.working_properties.keys() if k not in known_keys and k.lower() not in skip_keys]
        for k in sorted(extra_keys, key=lambda x: x.lower()):
            adv_fields.append({"key": k, "label": k, "type": "text"})
            
        # --- NEW FIX: Reorder Reference, parse URLs, and alias Description fields ---
        for f in adv_fields:
            if f["key"] == "Description_1":
                f["label"] = "Description"
            elif f["key"] == "Description_1_1":
                f["label"] = "Detailed Description"
                
            if f["key"].lower() in ["url", "digikey url", "mouser url", "link"]:
                f["type"] = "url"

        # Dynamically inject "Reference" into the preferred location
        ref_idx = next((i for i, f in enumerate(adv_fields) if f["key"].lower() == "reference"), -1)
        if ref_idx != -1:
            ref_field = adv_fields.pop(ref_idx)
            target_idx = -1
            
            # Place exactly one slot below Operating Temp
            for i, f in enumerate(adv_fields):
                k_low = f["key"].lower()
                l_low = f.get("label", "").lower()
                if "operating temp" in k_low or "operating temp" in l_low:
                    target_idx = i + 1
                    
            # Fallback: Place right above Suppliers
            if target_idx == -1:
                for i, f in enumerate(adv_fields):
                    if "supplier" in f["key"].lower():
                        target_idx = i
                        break
                        
            # Final fallback: Place at the top of advanced fields
            if target_idx == -1:
                target_idx = 0
                
            adv_fields.insert(target_idx, ref_field)
            
        self._build_fields(adv_fields, adv_layout)
        
        scroll = QScrollArea()
        scroll.setWidget(self.advanced_form)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        main_layout.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        
        self.btn_prev = QPushButton("Previous")
        self.btn_prev.clicked.connect(self.handle_prev)
        self.btn_next = QPushButton("Next")
        self.btn_next.clicked.connect(self.handle_next)
        
        if not self.is_edit:
            self.btn_prev.hide()
            self.btn_next.hide()
            
        btn_layout.addWidget(self.btn_prev)
        btn_layout.addWidget(self.btn_next)
        
        self.status_label = QLabel("")
        btn_layout.addWidget(self.status_label)
        btn_layout.addStretch()
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.clicked.connect(self.handle_apply)
        if not self.is_edit:
            self.btn_apply.hide()
            
        self.btn_save = QPushButton("Save && Close" if self.is_edit else "Import Component")
        self.btn_save.setProperty("action", "primary")
        self.btn_save.clicked.connect(self.handle_save)
        
        if self.is_batch:
            btn_abort = QPushButton("Abort Batch")
            btn_abort.setProperty("action", "danger")
            btn_abort.clicked.connect(self.handle_abort_batch)
            btn_layout.addWidget(btn_abort)
            
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(self.btn_apply)
        btn_layout.addWidget(self.btn_save)
        main_layout.addLayout(btn_layout)

    def has_unsaved_changes(self) -> bool:
        """Returns True only if the user explicitly interacted with the dialog's inputs or dropzones."""
        return getattr(self, '_is_dirty', False)

    def check_discard_changes(self) -> bool:
        if self.has_unsaved_changes():
            reply = QMessageBox.question(
                self, "Unsaved Changes", 
                "You have unsaved changes.\nAre you sure you want to discard them?", 
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
            )
            return reply == QMessageBox.StandardButton.Discard
        return True

    def handle_prev(self):
        if not self.check_discard_changes(): return
        self.nav_intent = 'prev'
        self.reject()

    def handle_next(self):
        if not self.check_discard_changes(): return
        self.nav_intent = 'next'
        self.reject()
        
    def handle_apply(self):
        self.nav_intent = None
        if self.save_callback:
            if self.save_callback(self):
                self.original_name = self.get_part_name()
                self.initial_state_props = self.get_updated_properties()
                self._is_dirty = False
                self.show_status("Changes applied successfully.")

    def close_viewer(self):
        if self.viewer_panel.isVisible():
            self.viewer_panel.setVisible(False)
            handle_w = self.splitter.handleWidth()
            self.resize(self.width() - 450 - handle_w, self.height())

    def view_symbol(self):
        if not self.viewer_panel.isVisible():
            w0 = self.left_panel.width()
            handle_w = self.splitter.handleWidth()
            self.resize(self.width() + 450 + handle_w, self.height())
            self.viewer_panel.setVisible(True)
            self.splitter.setSizes([w0, 450])
        self.viewer_panel.clear()
        self.viewer_panel.draw_symbol(self.symbol.raw_graphics_block)
        QTimer.singleShot(10, self.viewer_panel.fit_view)

    def view_footprint(self):
        fp_str = self.footprint_drop.file_path
        if not fp_str:
            fp_str = self.working_properties.get("Footprint", "")
            
        if not fp_str:
            QMessageBox.information(self, "No Footprint", "There is no footprint loaded to view.")
            return

        fp_path = Path(fp_str)
        if not fp_path.exists() and ":" in fp_str:
            cat, fp_name = fp_str.split(":", 1)
            lib_root = Path(get_setting_str(self.settings, "library_root"))
            fp_path = lib_root / "Footprints" / f"{cat}.pretty" / f"{fp_name}.kicad_mod"

        if not fp_path.exists():
            QMessageBox.warning(self, "Not Found", f"Could not locate footprint file:\n{fp_str}")
            return
            
        if not self.viewer_panel.isVisible():
            w0 = self.left_panel.width()
            handle_w = self.splitter.handleWidth()
            self.resize(self.width() + 450 + handle_w, self.height())
            self.viewer_panel.setVisible(True)
            self.splitter.setSizes([w0, 450])
            
        self.viewer_panel.clear()
        self.viewer_panel.draw_footprint(str(fp_path), "standard")
        QTimer.singleShot(10, self.viewer_panel.fit_view)

    def handle_abort_batch(self):
        self.abort_batch = True
        self.reject()

    def check_duplicate_name(self, text):
        clean_text = text.strip()
        if get_setting_bool(self.settings, "sanitize_names", True):
            clean_text = FileImporter.sanitize_name(clean_text)
            
        if self.is_edit and clean_text == self.original_name:
            self.dup_warning_lbl.hide()
            self.part_name_input.setProperty("error", False)
            self.btn_save.setEnabled(True)
        elif clean_text in self.cached_names:
            if self.is_edit:
                self.dup_warning_lbl.setText(f"⚠️ Warning: '{clean_text}' already exists. Overwriting is not supported from this dialog.")
                self.btn_save.setEnabled(False)
            else:
                self.dup_warning_lbl.setText(f"⚠️ Warning: '{clean_text}' already exists. Continuing will trigger the Merge resolver.")
                self.btn_save.setEnabled(True)
                
            self.dup_warning_lbl.show()
            self.part_name_input.setProperty("error", True)
        else:
            self.dup_warning_lbl.hide()
            self.part_name_input.setProperty("error", False)
            self.btn_save.setEnabled(True)
            
        self.part_name_input.style().unpolish(self.part_name_input)
        self.part_name_input.style().polish(self.part_name_input)

    def populate_initial_data(self):
        self.part_name_input.setText(self.symbol.name)
        
        prefix = get_library_prefix(self.settings)
        cat_clean = self.symbol.category[len(prefix):] if self.symbol.category.startswith(prefix) else self.symbol.category
        
        if self.category_combo.findText(cat_clean) == -1:
            self.category_combo.addItem(cat_clean)
            
        self.category_combo.setCurrentText(cat_clean)
        
        fp = self.prefilled_assets.get('footprint') or self.working_properties.get("Footprint", "")
        if fp:
            if ":" in str(fp) and not Path(str(fp)).exists():
                cat, fp_name = str(fp).split(":", 1)
                lib_root = Path(get_setting_str(self.settings, "library_root"))
                fp_path = lib_root / "Footprints" / f"{cat}.pretty" / f"{fp_name}.kicad_mod"
                if fp_path.exists():
                    self.footprint_drop.set_file(str(fp_path))
                    self.footprint_drop.is_library_link = True
            elif Path(str(fp)).exists():
                self.footprint_drop.set_file(str(fp))

        mdl = self.prefilled_assets.get('model') or self.working_properties.get("3D_Model", "") or self.working_properties.get("3D Model", "")
        if mdl: 
            if Path(str(mdl)).exists():
                self.model_drop.set_file(str(mdl))
            else:
                lib_root = Path(get_setting_str(self.settings, "library_root"))
                clean_mdl = str(mdl).replace("${KIPRJMOD}/", "").replace("${KICAD6_3DMODEL_DIR}/", "").replace("${KICAD7_3DMODEL_DIR}/", "")
                mdl_path = lib_root / clean_mdl
                if mdl_path.exists():
                    self.model_drop.set_file(str(mdl_path))
        
        ds = self.prefilled_assets.get('datasheet') or self.working_properties.get("Datasheet", "")
        if ds: 
            if Path(str(ds)).exists():
                self.datasheet_drop.set_file(str(ds))
            elif str(ds).startswith("http"):
                pass 
            else:
                lib_root = Path(get_setting_str(self.settings, "library_root"))
                ds_path = lib_root / str(ds)
                if ds_path.exists():
                    self.datasheet_drop.set_file(str(ds_path))

        img = self.prefilled_assets.get('image') or self.working_properties.get("Image_File", "")
        if img: 
            if Path(str(img)).exists():
                self.working_properties["Image_File"] = str(img)
            elif str(img).startswith("http"):
                pass 
            else:
                lib_root = Path(get_setting_str(self.settings, "library_root"))
                img_path = lib_root / str(img)
                if img_path.exists():
                    self.working_properties["Image_File"] = str(img_path)
        
        for k, v in self.working_properties.items():
            target_k = k
            if k.lower() == "description": target_k = "Description"
            elif k.lower() in ["supplier part", "supplier_part"]: target_k = "DigiKey_PN"
            
            widget = self.inputs.get(target_k)
            if widget is None:
                for inp_k, w in self.inputs.items():
                    if inp_k.lower() == target_k.lower():
                        widget = w
                        break
                        
            if widget is not None and isinstance(widget, QLineEdit):
                widget.setText(str(v))
                
        self._update_image_preview()
        
        self.original_name = self.get_part_name()
        self.initial_state_props = self.get_updated_properties()
        
        # Unlock the UI and reset the dirty flag ONLY when fully initialized
        self._init_complete = True
        self._is_dirty = False

    def _update_image_preview(self):
        img_path = self.working_properties.get("Image_File")
            
        if img_path and Path(img_path).exists():
            self.img_lbl.set_image_path(img_path)
            self.btn_clear_img.show()
        else:
            self.img_lbl.clear_image()
            self.btn_clear_img.hide()

    def show_status(self, message: str, is_error: bool = False):
        color = "#F14C4C" if is_error else "#0F9D58"
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.status_label.setText(message)
        if not is_error:
            QTimer.singleShot(4000, lambda: self.status_label.setText(""))

    def fetch_digikey_data(self):
        if not self.dk_api.is_configured():
            QMessageBox.information(self, "Not Configured", "Digi-Key API is not configured. Please add your credentials in Settings.")
            return
            
        terms_to_try = []
        mpn_widget = self.inputs.get("MPN")
        if isinstance(mpn_widget, QLineEdit) and mpn_widget.text().strip() and mpn_widget.text().strip() != "-":
            terms_to_try.append(mpn_widget.text().strip())
            
        dk_widget = self.inputs.get("DigiKey_PN")
        if isinstance(dk_widget, QLineEdit) and dk_widget.text().strip() and dk_widget.text().strip() != "-":
            terms_to_try.append(dk_widget.text().strip())
            
        part_name = self.part_name_input.text().strip()
        if part_name:
            terms_to_try.append(part_name)
            
        terms_to_try = list(dict.fromkeys(terms_to_try))
            
        if not terms_to_try:
            QMessageBox.warning(self, "Empty Field", "Please enter an MPN, Digi-Key PN, or Part Name to search.")
            return
            
        self.dk_btn.setText("Fetching...")
        self.dk_btn.setEnabled(False)
        
        dk_data = None
        for term in terms_to_try:
            self.show_status(f"Querying Digi-Key for '{term}'...")
            QApplication.processEvents()
            
            dk_data = self.dk_api.search_part(term)
            if dk_data:
                break
                
            self.show_status(f"'{term}' not found. Trying next...", is_error=True)
            QApplication.processEvents()
            QThread.msleep(800)
            
        if dk_data:
            for k, v in dk_data.items():
                widget = self.inputs.get(k)
                if widget is not None and isinstance(widget, QLineEdit):
                    widget.setText(str(v))
            
            if dk_data.get("Datasheet") and not self.datasheet_drop.file_path and self.tmp_dir:
                dest_pdf = self.tmp_dir / f"{self.get_part_name()}_Datasheet.pdf"
                if self.dk_api.download_datasheet(dk_data["Datasheet"], dest_pdf):
                    self.datasheet_drop.set_file(str(dest_pdf))
                    
            if dk_data.get("Image_URL") and self.tmp_dir:
                img_url = dk_data["Image_URL"]
                img_ext = ".jpg" if ".jpg" in img_url.lower() else ".png"
                dest_img = self.tmp_dir / f"{self.get_part_name()}_Image{img_ext}"
                try:
                    req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                    img_data = urllib.request.urlopen(req, timeout=10).read()
                    with open(dest_img, 'wb') as f: f.write(img_data)
                    self.working_properties["Image_File"] = str(dest_img)
                    if hasattr(self, 'img_lbl'):
                        self.img_lbl.set_image_path(str(dest_img))
                        self.btn_clear_img.show()
                except Exception as e:
                    logger.error(f"Failed to fetch image: {e}")
                    
            self.dk_btn.setText("🪄 Success!")
            self.show_status("Data successfully fetched from Digi-Key!")
            if not self.advanced_form.isVisible():
                self.toggle_advanced()
        else:
            self.dk_btn.setText("🪄 Not Found")
            self.show_status("Part not found on Digi-Key.", is_error=True)
            QThread.msleep(500)
            
            dialog = DigiKeyNotFoundDialog(self, terms_to_try[0] if terms_to_try else "")
            if dialog.exec() == QDialog.DialogCode.Accepted:
                new_kw = dialog.get_new_keyword()
                if new_kw:
                    if isinstance(dk_widget, QLineEdit):
                        dk_widget.setText(new_kw)
                    self.dk_btn.setText("🪄 Auto-Fill (Digi-Key)")
                    self.dk_btn.setEnabled(True)
                    return self.fetch_digikey_data()
            
        QThread.msleep(2000)
        self.dk_btn.setText("🪄 Auto-Fill (Digi-Key)")
        self.dk_btn.setEnabled(True)

    def toggle_advanced(self):
        if self.advanced_form.isVisible():
            self.advanced_form.hide()
            self.btn_toggle_adv.setText("▼ Show Advanced Properties")
        else:
            self.advanced_form.show()
            self.btn_toggle_adv.setText("▲ Hide Advanced Properties")

    def get_updated_properties(self) -> dict:
        props = dict(self.working_properties)
        for k, widget in self.inputs.items():
            if isinstance(widget, QLineEdit):
                val = widget.text().strip()
                if val: props[k] = val
                elif k in props: del props[k]
            elif isinstance(widget, QComboBox):
                val = widget.currentText().strip()
                if val: props[k] = val
                elif k in props: del props[k]
                
        if self.model_drop.file_path:
            props["3D_Model"] = self.model_drop.file_path
        elif not self.model_drop.file_path and getattr(self.model_drop, "cleared_by_user", False):
            props.pop("3D_Model", None)
            props.pop("3D Model", None)
            
        if self.footprint_drop.file_path and not self.footprint_drop.is_library_link:
            props["Footprint"] = self.footprint_drop.file_path
        elif not self.footprint_drop.file_path and getattr(self.footprint_drop, "cleared_by_user", False):
            props.pop("Footprint", None)
            
        if self.datasheet_drop.file_path:
            props["Datasheet"] = self.datasheet_drop.file_path
        elif not self.datasheet_drop.file_path and getattr(self.datasheet_drop, "cleared_by_user", False):
            props.pop("Datasheet", None)

        if self.img_lbl.file_path:
            props["Image_File"] = self.img_lbl.file_path
        elif not self.img_lbl.file_path:
            props.pop("Image_File", None)
            
        return sync_vendor_part_numbers(props)
        
    def get_part_name(self) -> str:
        name = self.part_name_input.text().strip()
        if get_setting_bool(self.settings, "sanitize_names", True):
            return FileImporter.sanitize_name(name)
        return name

    def get_category(self) -> str:
        return self.category_combo.currentText().strip()

    def handle_save(self):
        has_fp = False
        if self.footprint_drop.file_path and Path(self.footprint_drop.file_path).exists():
            has_fp = True
        elif self.working_properties.get("Footprint"):
            fp_str = self.working_properties.get("Footprint", "")
            if ":" in fp_str:
                cat, fp_name = fp_str.split(":", 1)
                lib_root_str = get_setting_str(self.settings, "library_root")
                if lib_root_str:
                    fp_path = Path(lib_root_str) / "Footprints" / f"{cat}.pretty" / f"{fp_name}.kicad_mod"
                    if fp_path.exists():
                        has_fp = True
            elif Path(fp_str).exists():
                has_fp = True

        if not has_fp:
            reply = QMessageBox.warning(
                self, "Missing Footprint",
                "You have not linked a valid footprint (.kicad_mod) file to this component.\n\n"
                "If you continue, the component will be saved but will be flagged as 'Broken Link' in the Health Scanner until a valid footprint is added.\n\n"
                "Do you want to save this component anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        self.nav_intent = None
        if self.save_callback:
            if self.save_callback(self):
                self.accept()
        else:
            self.accept()


# ==========================================
# MERGE RESOLUTION DIALOG
# ==========================================

class MergeComparisonDialog(QDialog):
    def __init__(self, parent=None, left_sym: Optional[Symbol]=None, right_sym: Optional[Symbol]=None, reasons=None):
        super().__init__(parent)
        self.setWindowTitle("Merge Conflict Resolution")
        
        self.resize(950, 750)
        
        self.left_sym = left_sym or Symbol("Unknown", "Uncategorized")
        self.right_sym = right_sym or Symbol("Unknown", "Uncategorized")
        
        self.reasons = reasons or []
        self.settings = QSettings("OpenSourceTools", "KiCadLibManager")
        
        self.left_widgets: Dict[str, QWidget] = {}
        self.selections: Dict[str, str] = {}
        self.status_labels: Dict[str, QLabel] = {}
        self.required_fields = set()
        self.pin_diffs: List[str] = []
        self.final_action = None
        
        self._calculate_pin_diffs()
        
        self.fields = ["Category", "Part Name", "Base Symbol Graphics"]
        known_keys = [f["key"] for section in PART_FIELDS.values() for f in section]
        all_props = set(self.left_sym.properties.keys()).union(set(self.right_sym.properties.keys()))
        
        trivial_fields = ["uuid", "suggested_category"]
        
        for k in known_keys:
            if k in all_props and k.lower() not in trivial_fields:
                self.fields.append(k)
                
        for k in sorted(list(all_props)):
            if k not in self.fields and k.lower() not in trivial_fields:
                self.fields.append(k)
                
        self.init_ui()

    def _calculate_pin_diffs(self):
        new_pins = { p.number: p for p in self.left_sym.pins }
        old_pins = { p.number: p for p in self.right_sym.pins }
        all_nums = sorted(set(new_pins.keys()).union(set(old_pins.keys())))
        
        diffs = []
        for num in all_nums:
            np = new_pins.get(num)
            op = old_pins.get(num)
            if np is None and op is not None: diffs.append(f"Pin {num} -> MISSING in New Part")
            elif op is None and np is not None: diffs.append(f"Pin {num} -> MISSING in Existing Part")
            elif np is not None and op is not None:
                if np.name != op.name or np.direction != op.direction or np.at != op.at:
                    diffs.append(f"Pin {num} differs ({np.name} vs {op.name}).")
                    
        if len(diffs) > 5:
            diffs = diffs[:5] + [f"...and {len(diffs)-5} more pin differences."]
        self.pin_diffs = diffs

    def _make_thumb(self, path: str) -> QLabel:
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if path and Path(path).exists():
            pm = QPixmap(path)
            if not pm.isNull():
                lbl.setPixmap(pm.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                return lbl
        lbl.setText("No Image")
        return lbl

    def _on_category_changed(self, text: str):
        self._update_subcategories(text)

    def _update_subcategories(self, cat_name: str, set_val: str = ""):
        combo = self.left_widgets.get("Subcategory")
        if not isinstance(combo, QComboBox): return
        
        combo.blockSignals(True)
        combo.clear()
        
        subcats = get_active_subcategories(self.settings).get(cat_name, [])
        combo.addItem("")
        combo.addItems(sorted(subcats))
        combo.insertSeparator(combo.count())
        combo.addItem("Add New Sub Category...")
        
        if set_val and set_val not in subcats and set_val != "Add New Sub Category...":
            combo.insertItem(combo.count() - 2, set_val)
            
        if set_val: combo.setCurrentText(set_val)
        else: combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def check_new_subcategory(self, text: str):
        combo = self.left_widgets.get("Subcategory")
        if not isinstance(combo, QComboBox): return
        
        if text == "Add New Sub Category...":
            new_subcat, ok = QInputDialog.getText(self, "New Sub Category", "Enter new subcategory:")
            if ok and new_subcat.strip():
                new_subcat = new_subcat.strip()
                subcats_dict = get_active_subcategories(self.settings)
                
                cat_w = self.left_widgets.get("Category")
                cat_name = cat_w.currentText() if isinstance(cat_w, QComboBox) else ""
                
                if cat_name not in subcats_dict: subcats_dict[cat_name] = []
                if new_subcat not in subcats_dict[cat_name]:
                    subcats_dict[cat_name].append(new_subcat)
                    self.settings.setValue("custom_subcategories", json.dumps(subcats_dict))
                
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("")
                combo.addItems(sorted(subcats_dict[cat_name]))
                combo.insertSeparator(combo.count())
                combo.addItem("Add New Sub Category...")
                combo.setCurrentText(new_subcat)
                combo.blockSignals(False)
            else:
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)

    def _are_assets_identical(self, field: str, s_l: str, s_r: str) -> bool:
        if field not in ["Footprint", "Datasheet", "3D_Model", "3D Model", "Image_File"]:
            return False
            
        if not s_l or not s_r: return False
        if str(s_l).startswith("http") or str(s_r).startswith("http"): return False
        
        lib_root_str = get_setting_str(self.settings, "library_root")
        if not lib_root_str: return False
        lib_root = Path(lib_root_str)
        path_var_raw = get_setting_str(self.settings, "kicad_path_var", "").strip()
        
        def resolve(val: str) -> Optional[Path]:
            p = Path(val)
            if p.exists() and p.is_file(): return p
            
            if field == "Footprint" and ":" in val:
                c, n = val.split(":", 1)
                p_try = lib_root / "Footprints" / f"{c}.pretty" / f"{n}.kicad_mod"
                if p_try.exists() and p_try.is_file(): return p_try
                
            clean_val = val.replace("${KIPRJMOD}/", "").replace("${KICAD6_3DMODEL_DIR}/", "").replace("${KICAD7_3DMODEL_DIR}/", "").replace("${KICAD8_3DMODEL_DIR}/", "")
            if path_var_raw: clean_val = clean_val.replace(f"{path_var_raw}/", "")
            
            p_try = lib_root / clean_val
            if p_try.exists() and p_try.is_file(): return p_try
            
            return None
            
        path_l = resolve(s_l)
        path_r = resolve(s_r)
        
        if path_l and path_r:
            if path_l.resolve() == path_r.resolve(): return True
            try:
                return filecmp.cmp(path_l, path_r, shallow=False)
            except Exception:
                return False
        return False

    def _evaluate_match_state(self, field: str, s_l: str, s_r: str) -> str:
        if field == "Base Symbol Graphics":
            return "conflict" if self.pin_diffs else "match"
            
        s_l = str(s_l).strip()
        s_r = str(s_r).strip()
        
        if s_l.lower() == s_r.lower():
            return "match"
            
        if self._are_assets_identical(field, s_l, s_r):
            return "match"
            
        if not s_l or not s_r or s_l.lower() in s_r.lower() or s_r.lower() in s_l.lower():
            return "additive"
            
        return "conflict"

    def init_ui(self):
        main_box = QVBoxLayout(self)
        main_box.setContentsMargins(10, 10, 10, 10)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_box.addWidget(self.splitter)
        
        self.left_panel = QWidget()
        main_layout = QVBoxLayout(self.left_panel)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.viewer_panel = KiCadViewerWidget()
        self.viewer_panel.setVisible(False)
        self.viewer_panel.preview_closed.connect(self.close_viewer)
        
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.viewer_panel)
        self.splitter.setStretchFactor(0, 6)
        self.splitter.setStretchFactor(1, 4)

        self.btn_merge = QPushButton("Merge && Overwrite")
        self.btn_merge.setProperty("action", "primary")
        self.btn_merge.clicked.connect(lambda: self.finalize('merge'))
        self.btn_merge.setEnabled(False)
        
        self.btn_keep_both = QPushButton("Keep Both (Add as New)")
        self.btn_keep_both.clicked.connect(lambda: self.finalize('add_new'))
        
        header_layout = QHBoxLayout()
        reason_lbl = QLabel(f"<b>Conflict Detected!</b><br><small>Matched on: {', '.join(self.reasons)}</small>")
        reason_lbl.setProperty("cssClass", "header_lbl")
        header_layout.addWidget(reason_lbl)
        
        header_layout.addStretch()
        self.btn_view_diff = QPushButton("👁 Compare Symbol Graphics")
        self.btn_view_diff.clicked.connect(self.view_graphics_diff)
        header_layout.addWidget(self.btn_view_diff)
        main_layout.addLayout(header_layout)
        
        main_layout.addWidget(QLabel("Select the best values from either side. Green highlights indicate your selections. <b>You can manually type overrides in the Left column.</b>"))
        
        scroll_widget = QWidget()
        self.grid = QGridLayout(scroll_widget)
        self.grid.setSpacing(10)
        
        self.grid.addWidget(QLabel("<b>Field</b>"), 0, 0)
        self.grid.addWidget(QLabel("<b>New Part</b>"), 0, 1)
        self.grid.addWidget(QLabel("<b>Existing Part</b>"), 0, 2)
        
        prefix = get_library_prefix(self.settings)
        cat1 = self.left_sym.category[len(prefix):] if self.left_sym.category.startswith(prefix) else self.left_sym.category
        cat2 = self.right_sym.category[len(prefix):] if self.right_sym.category.startswith(prefix) else self.right_sym.category

        self.cells = {}

        for row, field in enumerate(self.fields, start=1):
            if field == "Category":
                val_l, val_r = cat1, cat2
            elif field == "Part Name":
                val_l, val_r = self.left_sym.name, self.right_sym.name
            elif field == "Base Symbol Graphics":
                val_l, val_r = "", ""
            else:
                val_l = self.left_sym.properties.get(field, "")
                val_r = self.right_sym.properties.get(field, "")
                
            s_l = str(val_l).strip()
            s_r = str(val_r).strip()

            match_state = self._evaluate_match_state(field, s_l, s_r)
                
            lbl_field = QLabel()
            lbl_field.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.status_labels[field] = lbl_field
            self.grid.addWidget(lbl_field, row, 0)
            self._update_field_status_ui(field, match_state)
            
            if field == "Category":
                w_l = QComboBox()
                style_combobox_dropdown(w_l)
                w_l.addItems(sorted(get_active_categories(self.settings).keys()))
                w_l.setCurrentText(str(val_l))
                w_l.setStyleSheet("QComboBox { background: transparent; border: none; }")
                w_l.currentTextChanged.connect(lambda t, f=field: self.make_selection(f, 'left'))
                w_l.currentTextChanged.connect(lambda t, f=field: self.re_evaluate_field(f))
                w_l.currentTextChanged.connect(self._on_category_changed)
                self.left_widgets[field] = w_l
            elif field == "Part Name":
                w_l = QLineEdit(str(val_l))
                w_l.setCursorPosition(0)
                w_l.setStyleSheet("background: transparent; border: none;")
                w_l.textChanged.connect(lambda t, f=field: self.make_selection(f, 'left'))
                w_l.textChanged.connect(lambda t, f=field: self.re_evaluate_field(f))
                self.left_widgets[field] = w_l
            elif field == "Base Symbol Graphics":
                if match_state == "conflict":
                    diff_text = "<br>".join(self.pin_diffs)
                    w_l = QLabel(f"<b>Keep NEW Graphics & Pins</b><br><small>{diff_text}</small>")
                    w_l.setWordWrap(True)
                else:
                    w_l = QLabel("<b>Graphics & Pins Match</b>")
                self.left_widgets[field] = w_l
            elif field == "Image_File":
                w_l = self._make_thumb(str(val_l))
                self.left_widgets[field] = w_l
            else:
                f_type = "text"
                for section in PART_FIELDS.values():
                    for f in section:
                        if f["key"] == field: f_type = f.get("type", "text")
                
                if f_type == "subcategory_list" or field.lower() == "subcategory":
                    w_l = QComboBox()
                    style_combobox_dropdown(w_l)
                    self.left_widgets[field] = w_l
                    self._update_subcategories(cat1, str(val_l))
                    w_l.setStyleSheet("QComboBox { background: transparent; border: none; }")
                    w_l.currentTextChanged.connect(self.check_new_subcategory)
                    w_l.currentTextChanged.connect(lambda t, f=field: self.re_evaluate_field(f))
                else:
                    w_l = QLineEdit(str(val_l))
                    w_l.setCursorPosition(0)
                    w_l.setStyleSheet("background: transparent; border: none;")
                    w_l.textChanged.connect(lambda t, f=field: self.make_selection(f, 'left'))
                    w_l.textChanged.connect(lambda t, f=field: self.re_evaluate_field(f))
                    self.left_widgets[field] = w_l

            if field == "Base Symbol Graphics":
                if match_state == "conflict":
                    w_r = QLabel("<b>Keep EXISTING Graphics & Pins</b>")
                else:
                    w_r = QLabel("<b>Graphics & Pins Match</b>")
            elif field == "Image_File":
                w_r = self._make_thumb(str(val_r))
            else:
                w_r = QLabel(str(val_r))
                w_r.setWordWrap(True)

            cl = SelectionCell('left', w_l, match_state)
            cl.content = str(val_l)
            cr = SelectionCell('right', w_r, match_state)
            cr.content = str(val_r)
            
            cl.cellClicked.connect(lambda side, f=field: self.make_selection(f, side))
            cr.cellClicked.connect(lambda side, f=field: self.make_selection(f, side))

            self.grid.addWidget(cl, row, 1)
            self.grid.addWidget(cr, row, 2)
            self.cells[field] = {'left': cl, 'right': cr}
            
            if match_state == "additive" and len(s_r) > len(s_l):
                self.make_selection(field, 'right')
            else:
                self.make_selection(field, 'left')
            
        scroll = QScrollArea()
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        main_layout.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(self.btn_keep_both)
        btn_layout.addWidget(self.btn_merge)
        main_layout.addLayout(btn_layout)
        self._check_merge_ready()

    def _update_field_status_ui(self, field: str, match_state: str):
        lbl = self.status_labels.get(field)
        if not lbl: return

        if match_state == "conflict":
            self.required_fields.add(field)
            lbl.setText(f"<b>{field}</b> <span style='color: #F14C4C;'>≠</span>")
            lbl.setStyleSheet("background-color: rgba(241, 76, 76, 0.15); padding: 10px; border-radius: 4px;")
        elif match_state == "additive":
            self.required_fields.add(field) 
            lbl.setText(f"<b>{field}</b> <span style='color: #FD7E14;'>+</span>")
            lbl.setStyleSheet("background-color: rgba(253, 126, 20, 0.15); padding: 10px; border-radius: 4px;")
        else:
            if field in self.required_fields and field != "Base Symbol Graphics":
                self.required_fields.remove(field)
            lbl.setText(f"<b>{field}</b> <span style='color: #0F9D58;'>✓</span>")
            lbl.setStyleSheet("background-color: rgba(15, 157, 88, 0.10); padding: 10px; border-radius: 4px;")

        cell_pair = self.cells.get(field)
        if cell_pair:
            cell_pair['left'].set_match_state(match_state)
            cell_pair['right'].set_match_state(match_state)

    def re_evaluate_field(self, field: str):
        if field in ["Base Symbol Graphics", "Image_File"]: return

        w_l = self.left_widgets.get(field)
        if not w_l: return

        if isinstance(w_l, QComboBox): s_l = w_l.currentText().strip()
        elif isinstance(w_l, QLineEdit): s_l = w_l.text().strip()
        else: s_l = ""

        if field == "Category":
            prefix = get_library_prefix(self.settings)
            s_r = self.right_sym.category[len(prefix):] if self.right_sym.category.startswith(prefix) else self.right_sym.category
        elif field == "Part Name":
            s_r = self.right_sym.name
        else:
            s_r = self.right_sym.properties.get(field, "")

        match_state = self._evaluate_match_state(field, s_l, s_r)

        self._update_field_status_ui(field, match_state)
        self._check_merge_ready()

    def close_viewer(self):
        if self.viewer_panel.isVisible():
            self.viewer_panel.setVisible(False)
            handle_w = self.splitter.handleWidth()
            self.resize(self.width() - 500 - handle_w, self.height())

    def view_graphics_diff(self):
        if not self.viewer_panel.isVisible():
            w0 = self.left_panel.width()
            handle_w = self.splitter.handleWidth()
            self.resize(self.width() + 500 + handle_w, self.height())
            self.viewer_panel.setVisible(True)
            self.splitter.setSizes([w0, 500])
        self.viewer_panel.overlay_symbols(self.left_sym.raw_graphics_block, self.right_sym.raw_graphics_block)
        QTimer.singleShot(10, self.viewer_panel.fit_view)

    def make_selection(self, field: str, side: str):
        self.selections[field] = side
        self.cells[field]['left'].set_selected(side == 'left')
        self.cells[field]['right'].set_selected(side == 'right')
        self._check_merge_ready()

    def _check_merge_ready(self):
        is_ready = all(r in self.selections for r in self.required_fields)
        if hasattr(self, 'btn_merge'):
            self.btn_merge.setEnabled(is_ready)

    def finalize(self, action):
        self.final_action = action
        self.accept()

    def get_merged_data(self):
        props = {}
        for field, side in self.selections.items():
            if field in ["Category", "Part Name", "Base Symbol Graphics", "Image_File"]: 
                continue
                
            w_l = self.left_widgets.get(field)
            if side == 'left' and w_l:
                if isinstance(w_l, QComboBox): val = w_l.currentText().strip()
                elif isinstance(w_l, QLineEdit): val = w_l.text().strip()
                else: val = self.cells[field]['left'].content
            else:
                val = self.cells[field]['right'].content
                
            if val: props[field] = val

        cat_side = self.selections.get("Category", "left")
        w_cat = self.left_widgets.get("Category")
        if cat_side == "left" and isinstance(w_cat, QComboBox): 
            cat_val = w_cat.currentText().strip()
        else: 
            prefix = get_library_prefix(self.settings)
            cat_val = self.right_sym.category[len(prefix):] if self.right_sym.category.startswith(prefix) else self.right_sym.category
        
        name_side = self.selections.get("Part Name", "left")
        w_name = self.left_widgets.get("Part Name")
        if name_side == "left" and isinstance(w_name, QLineEdit):
            name_val = w_name.text().strip()
        else:
            name_val = self.right_sym.name
            
        base_side = self.selections.get("Base Symbol Graphics", "left")
        
        return {
            'category': cat_val,
            'name': name_val,
            'base_symbol': base_side,
            'props': sync_vendor_part_numbers(props)
        }

# ==========================================
# SUPPORTING DIALOGS
# ==========================================

class FootprintChooserDialog(QDialog):
    def __init__(self, parent, lib_root: Path, cat_name: str, current_fp: str):
        super().__init__(parent)
        self.setWindowTitle("Select Footprint from Library")
        self.resize(500, 450)
        self.selected_filepath = None
        self.lib_root = lib_root
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("<b>Category Folder:</b>"))
        self.cat_combo = QComboBox()
        style_combobox_dropdown(self.cat_combo)
        
        cats = []
        if (lib_root / "Footprints").exists():
            for d in (lib_root / "Footprints").glob("*.pretty"):
                if d.is_dir(): cats.append(d.name.replace('.pretty', ''))
                
        self.cat_combo.addItems(sorted(cats))
        
        idx = self.cat_combo.findText(cat_name)
        if idx >= 0: self.cat_combo.setCurrentIndex(idx)
            
        layout.addWidget(self.cat_combo)
        
        layout.addWidget(QLabel("<b>Available Footprints:</b>"))
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        
        self.cat_combo.currentTextChanged.connect(self._populate_list)
        self._populate_list(self.cat_combo.currentText())
        
        if current_fp:
            items = self.list_widget.findItems(current_fp + ".kicad_mod", Qt.MatchFlag.MatchExactly)
            if items:
                self.list_widget.setCurrentItem(items[0])
        
        self.list_widget.itemDoubleClicked.connect(self._on_select)
        
        btn_box = QHBoxLayout()
        btn_ok = QPushButton("Select Footprint")
        btn_ok.setProperty("action", "primary")
        btn_ok.clicked.connect(self._on_select)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_box.addStretch()
        btn_box.addWidget(btn_cancel)
        btn_box.addWidget(btn_ok)
        layout.addLayout(btn_box)

    def _populate_list(self, cat):
        self.list_widget.clear()
        fp_dir = self.lib_root / "Footprints" / f"{cat}.pretty"
        if fp_dir.exists():
            fps = [f.name for f in fp_dir.glob("*.kicad_mod")]
            self.list_widget.addItems(sorted(fps))

    def _on_select(self):
        item = self.list_widget.currentItem()
        if item:
            cat = self.cat_combo.currentText()
            self.selected_filepath = str(self.lib_root / "Footprints" / f"{cat}.pretty" / item.text())
            self.accept()

class PartSelectionDialog(QDialog):
    def __init__(self, parent, cached_names):
        super().__init__(parent)
        self.setWindowTitle("Link Existing Parts")
        self.resize(400, 500)
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select one or more existing parts from the library:"))
        
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search parts...")
        layout.addWidget(self.search)
        
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.addItems(sorted(list(cached_names)))
        layout.addWidget(self.list)
        
        self.search.textChanged.connect(self._filter)
        
        btns = QHBoxLayout()
        btn_ok = QPushButton("Link Selected")
        btn_ok.setProperty("action", "primary")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        layout.addLayout(btns)
        
    def _filter(self, text):
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text.lower() not in item.text().lower())
            
    def get_selected_parts(self):
        return [item.text() for item in self.list.selectedItems()]

class DigiKeyNotFoundDialog(QDialog):
    def __init__(self, parent, term):
        super().__init__(parent)
        self.setWindowTitle("Part Not Found")
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Could not find '{term}' on Digi-Key.\nEnter a different keyword or MPN to try again:"))
        
        self.input = QLineEdit()
        self.input.setText(term)
        layout.addWidget(self.input)
        
        btns = QHBoxLayout()
        btn_ok = QPushButton("Try Again")
        btn_ok.setProperty("action", "primary")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        layout.addLayout(btns)
        
    def get_new_keyword(self):
        return self.input.text().strip()

class LinkFilesDialog(QDialog):
    def __init__(self, parent, filename, cached_names):
        super().__init__(parent)
        self.setWindowTitle("Link Standalone Asset")
        self.resize(450, 200)
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Select a symbol to link <b>{filename}</b> to:"))
        
        self.combo = QComboBox()
        style_combobox_dropdown(self.combo)
        self.combo.addItems(sorted(list(cached_names)))
        layout.addWidget(self.combo)
        
        self.chk_default = QCheckBox("Set as default footprint for this symbol")
        self.chk_default.setChecked(True)
        layout.addWidget(self.chk_default)
        
        layout.addStretch()
        
        btns = QHBoxLayout()
        btn_ok = QPushButton("Link File")
        btn_ok.setProperty("action", "primary")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Skip File")
        btn_cancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        layout.addLayout(btns)
        
    def get_selected_symbol(self):
        return self.combo.currentText()
        
    @property
    def set_default(self):
        return self.chk_default.isChecked()

class BackupRestoreDialog(QDialog):
    def __init__(self, parent, backups, generate_diff_fn):
        super().__init__(parent)
        self.setWindowTitle("Restore Library Backup")
        self.resize(850, 550)
        self.selected_backup = None
        self.generate_diff_fn = generate_diff_fn
        
        layout = QHBoxLayout(self)
        
        left_panel = QVBoxLayout()
        left_panel.addWidget(QLabel("<b>Available Backups:</b>"))
        self.list = QListWidget()
        for b in sorted(backups, key=os.path.getmtime, reverse=True):
            self.list.addItem(str(b))
        left_panel.addWidget(self.list)
        layout.addLayout(left_panel, 2)
        
        right_panel = QVBoxLayout()
        right_panel.addWidget(QLabel("<b>Backup Differences:</b>"))
        self.diff_view = QTextBrowser()
        right_panel.addWidget(self.diff_view)
        
        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_restore = QPushButton("Restore Selected Backup")
        btn_restore.setProperty("action", "danger")
        btn_restore.clicked.connect(self._on_restore)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_restore)
        right_panel.addLayout(btn_layout)
        
        layout.addLayout(right_panel, 3)
        self.list.currentTextChanged.connect(self._update_diff)
        
        if self.list.count() > 0:
            self.list.setCurrentRow(0)
        
    def _update_diff(self, path_str):
        if path_str:
            self.diff_view.setHtml(self.generate_diff_fn(Path(path_str)))
            
    def _on_restore(self):
        if self.list.currentItem():
            reply = QMessageBox.warning(
                self, "Confirm Restore", 
                "Are you sure you want to revert this library file?\n\nAny components added since this backup was taken will be permanently lost.", 
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.selected_backup = Path(self.list.currentItem().text())
                self.accept()

class DigestDialog(QDialog):
    def __init__(self, parent, title, successes, skips, failures, theme):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(550, 450)
        
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        
        html = f"<h2>{title} Results</h2>"
        
        if successes:
            html += f"<h3 style='color: #0F9D58;'>Successfully Imported ({len(successes)})</h3><ul>"
            for s in successes: html += f"<li>{s}</li>"
            html += "</ul>"
            
        if skips:
            html += f"<h3 style='color: #FD7E14;'>Skipped ({len(skips)})</h3><ul>"
            for s in skips: html += f"<li>{s}</li>"
            html += "</ul>"
            
        if failures:
            html += f"<h3 style='color: #F14C4C;'>Errors ({len(failures)})</h3><ul>"
            for f in failures: html += f"<li><b>{f[0]}</b>: {f[1]}</li>"
            html += "</ul>"
            
        browser.setHtml(html)
        layout.addWidget(browser)
        
        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)

class LogViewerDialog(QDialog):
    def __init__(self, log_path, parent):
        super().__init__(parent)
        self.setWindowTitle("Application Logs")
        self.resize(800, 600)
        
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        
        if log_path.exists():
            with open(log_path, 'r', encoding='utf-8') as f:
                browser.setPlainText(f.read())
                
        browser.verticalScrollBar().setValue(browser.verticalScrollBar().maximum())
        
        layout.addWidget(browser)
        
        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)

class OrphanResolverDialog(QDialog):
    def __init__(self, parent, orphans, active_categories):
        super().__init__(parent)
        self.setWindowTitle("Resolve Uncategorized Parts")
        self.resize(750, 450)
        self.orphans = orphans
        self.active_categories = active_categories
        self.mappings = {}
        
        main_layout = QVBoxLayout(self)
        main_layout.addWidget(QLabel("The following components are in folders not mapped to your active Top-Level categories.\nPlease assign them to a valid category so they show up correctly in the browser."))
        
        table = QTableWidget(len(orphans), 3)
        table.setHorizontalHeaderLabels(["Part Name", "Description", "New Category"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        
        for row, orphan in enumerate(self.orphans):
            table.setItem(row, 0, QTableWidgetItem(orphan['name']))
            table.setItem(row, 1, QTableWidgetItem(orphan.get('desc', '')))
            
            combo = QComboBox()
            style_combobox_dropdown(combo)
            combo.addItems(["Uncategorized"] + sorted(self.active_categories))
            
            old_cat_str = orphan['old_cat'].name
            if old_cat_str in self.active_categories:
                combo.setCurrentText(old_cat_str)
            else:
                for c in self.active_categories:
                    if c.lower() in old_cat_str.lower() or old_cat_str.lower() in c.lower():
                        combo.setCurrentText(c)
                        break
                        
            self.mappings[orphan['name']] = combo
            table.setCellWidget(row, 2, combo)
            
        main_layout.addWidget(table)
        
        btns = QHBoxLayout()
        btn_apply = QPushButton("Apply && Remap")
        btn_apply.setProperty("action", "primary")
        btn_apply.clicked.connect(self.accept)
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_apply)
        main_layout.addLayout(btns)

    def get_mapping(self) -> dict:
        return {name: combo.currentText() for name, combo in self.mappings.items()}

class ManualDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workflow Manual")
        self.resize(750, 650)
        
        main_layout = QVBoxLayout(self)
        browser = QTextBrowser()
        
        # Load the HTML dynamically from our constants file!
        browser.setHtml(MANUAL_HTML)
        
        main_layout.addWidget(browser)
        
        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        main_layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
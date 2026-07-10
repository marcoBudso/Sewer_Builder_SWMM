import os
import math
import datetime
import subprocess
import re
import json

from qgis.PyQt.QtCore import Qt, QVariant, QSize
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QDoubleSpinBox, QLineEdit, QFileDialog, QMessageBox,
    QCheckBox, QGroupBox, QTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QSplitter, QScrollArea, QFormLayout, QGridLayout, QTabWidget, QWidget,
    QListWidget, QListWidgetItem, QDialogButtonBox
)
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsVertexMarker
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsWkbTypes, QgsFeature, QgsField, QgsFields,
    QgsGeometry, QgsPointXY, QgsSpatialIndex, QgsFeatureRequest,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsVectorFileWriter,
    QgsProcessingFeedback, QgsRasterLayer, QgsRaster,
    QgsGraduatedSymbolRenderer, QgsRendererRange, QgsSymbol
)
import processing


class BasinDrawTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, finished_callback, message_callback=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.finished_callback = finished_callback
        self.message_callback = message_callback
        self.points = []
        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber.setColor(QColor(0, 120, 255, 80))
        self.rubber.setFillColor(QColor(0, 120, 255, 40))
        self.rubber.setWidth(2)

    def canvasPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.button() == Qt.LeftButton:
            p = self.toMapCoordinates(event.pos())
            self.points.append(QgsPointXY(p))
            self._refresh()
            if self.message_callback:
                self.message_callback(f"Punti bacino: {len(self.points)}. Clic destro per chiudere.")
        elif event.button() == Qt.RightButton:
            self.finish_polygon()

    def canvasMoveEvent(self, event):
        # Display the polygon being sketched in real time, including the
        # current mouse position as a temporary vertex.
        if not self.points or self.rubber is None:
            return
        p = self.toMapCoordinates(event.pos())
        self._refresh(QgsPointXY(p))

    def keyPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.key() == Qt.Key_Escape:
            self.reset()
            if self.message_callback:
                self.message_callback('Catchment drawing cancelled.')
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_polygon()

    def _refresh(self, preview_point=None):
        if self.rubber is None:
            return
        self.rubber.reset(QgsWkbTypes.PolygonGeometry)
        if not self.points:
            return
        pts = list(self.points)
        if preview_point is not None:
            pts.append(QgsPointXY(preview_point))
        # Close the polygon while sketching so the user can preview the
        # catchment area that will be captured on right-click.
        if len(pts) > 2:
            pts.append(pts[0])
        for p in pts:
            self.rubber.addPoint(p, False)
        self.rubber.show()
        self.canvas.refresh()

    def reset(self):
        self.points = []
        if self.rubber:
            self.rubber.reset(QgsWkbTypes.PolygonGeometry)
        self.canvas.refresh()

    def clear_drawing(self):
        """Remove the temporary RubberBand polygon from the map canvas."""
        self.points = []
        if self.rubber:
            try:
                self.rubber.reset(QgsWkbTypes.PolygonGeometry)
                self.canvas.scene().removeItem(self.rubber)
            except Exception:
                pass
            self.rubber = None
        self.canvas.refresh()

    def finish_polygon(self):
        if len(self.points) < 3:
            if self.message_callback:
                self.message_callback('At least 3 points are required to create the catchment.')
            return
        geom = QgsGeometry.fromPolygonXY([self.points + [self.points[0]]])
        self.finished_callback(geom, self.canvas.mapSettings().destinationCrs())
        # After the right-click, the polygon remains stored in the plugin as
        # geometry, while the temporary map sketch is removed.
        self.clear_drawing()
        if self.message_callback:
            self.message_callback('Catchment polygon acquired and temporary sketch cleared.')


class ProjectTraceDrawTool(QgsMapToolEmitPoint):
    """Interactive map tool used to draw a proposed sewer alignment.

    While the mouse is moving, the tool displays the nearest snap point on
    existing conduits so the user can preview the connection before
    completing the sketch with a right-click.
    """
    def __init__(self, canvas, finished_callback, message_callback=None, snap_layer=None, snap_tolerance=2.0, snap_node_layer=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.finished_callback = finished_callback
        self.message_callback = message_callback
        self.snap_layer = snap_layer
        self.snap_node_layer = snap_node_layer
        self.snap_tolerance = float(snap_tolerance or 2.0)
        self.points = []
        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.rubber.setColor(QColor(255, 80, 0, 180))
        self.rubber.setWidth(3)

        self.snap_line = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.snap_line.setColor(QColor(0, 180, 0, 190))
        self.snap_line.setWidth(2)

        self.snap_marker = QgsVertexMarker(canvas)
        self.snap_marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.snap_marker.setIconSize(18)
        self.snap_marker.setPenWidth(4)
        self.snap_marker.setColor(QColor(0, 180, 0, 220))
        self.snap_marker.hide()

    def _transform_point(self, point, src_crs, dst_crs):
        if not src_crs or not dst_crs or src_crs == dst_crs:
            return QgsPointXY(point)
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        return QgsPointXY(tr.transform(QgsPointXY(point)))

    def _nearest_snap_point(self, p_canvas):
        """Return the nearest snap candidate by checking conduits and nodes.

        ``type='line'`` indicates a snap to a conduit, while ``type='node'``
        indicates a snap to a manhole or junction.

        If a node is within the snap tolerance, node snapping takes priority
        over line snapping. This keeps intermediate manholes placed on a
        conduit selectable, because the line snap could otherwise prevail
        when the distance to the conduit is zero.
        """
        try:
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            best_line = None
            best_node = None

            if self.snap_layer is not None:
                p_layer = self._transform_point(p_canvas, canvas_crs, self.snap_layer.crs())
                p_geom = QgsGeometry.fromPointXY(p_layer)
                for f in self.snap_layer.getFeatures():
                    g = f.geometry()
                    if not g or g.isEmpty():
                        continue
                    near = g.nearestPoint(p_geom)
                    if not near or near.isEmpty():
                        continue
                    d = near.distance(p_geom)
                    if best_line is None or d < best_line["distance"]:
                        near_pt_layer = QgsPointXY(near.asPoint())
                        near_pt_canvas = self._transform_point(near_pt_layer, self.snap_layer.crs(), canvas_crs)
                        best_line = {"point_canvas": near_pt_canvas, "distance": float(d), "type": "line"}

            if self.snap_node_layer is not None:
                p_node = self._transform_point(p_canvas, canvas_crs, self.snap_node_layer.crs())
                p_geom_node = QgsGeometry.fromPointXY(p_node)
                for f in self.snap_node_layer.getFeatures():
                    g = f.geometry()
                    if not g or g.isEmpty():
                        continue
                    try:
                        if g.isMultipart():
                            pts = g.asMultiPoint()
                            node_pt = QgsPointXY(pts[0]) if pts else None
                        else:
                            node_pt = QgsPointXY(g.asPoint())
                    except Exception:
                        node_pt = None
                    if node_pt is None:
                        continue
                    d = QgsGeometry.fromPointXY(node_pt).distance(p_geom_node)
                    if best_node is None or d < best_node["distance"]:
                        node_canvas = self._transform_point(node_pt, self.snap_node_layer.crs(), canvas_crs)
                        best_node = {"point_canvas": node_canvas, "distance": float(d), "type": "node"}

            # Prioritize nodes when they fall within the configured snap
            # tolerance. This allows snapping to intermediate manholes as
            # well as to conduit endpoints.
            if best_node is not None and best_node["distance"] <= self.snap_tolerance:
                return best_node
            if best_line is not None and best_line["distance"] <= self.snap_tolerance:
                return best_line
            # Outside the tolerance, still display the nearest candidate so
            # the red marker helps the user understand the snap target.
            if best_node is not None and best_line is not None:
                return best_node if best_node["distance"] <= best_line["distance"] else best_line
            return best_node or best_line
        except Exception:
            return None

    def canvasMoveEvent(self, event):
        if not getattr(self, "_active", True):
            return
        p = QgsPointXY(self.toMapCoordinates(event.pos()))
        snap = self._nearest_snap_point(p)

        # Display the proposed conduit in real time using the vertices already
        # clicked plus the current mouse position as a temporary vertex.
        preview_point = p

        if self.snap_line is not None:
            self.snap_line.reset(QgsWkbTypes.LineGeometry)

        if snap:
            ok = snap["distance"] <= self.snap_tolerance
            if ok and snap.get("type") == "node":
                color = QColor(0, 90, 255, 230)  # blue = snap to an existing node
            elif ok:
                color = QColor(0, 180, 0, 220)   # green = snap to a conduit
            else:
                color = QColor(220, 0, 0, 220)   # red = outside tolerance
            if self.snap_marker is not None:
                self.snap_marker.setColor(color)
                # Node = blue circle, conduit = green cross, so the user can
                # immediately identify the snap type.
                if snap.get("type") == "node":
                    self.snap_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
                    self.snap_marker.setIconSize(24)
                else:
                    self.snap_marker.setIconType(QgsVertexMarker.ICON_CROSS)
                    self.snap_marker.setIconSize(18)
                self.snap_marker.setCenter(snap["point_canvas"])
                self.snap_marker.show()
            if self.snap_line is not None:
                self.snap_line.setColor(color)
                self.snap_line.addPoint(p, False)
                self.snap_line.addPoint(snap["point_canvas"], True)
                self.snap_line.show()

            # If the point is within tolerance, end the preview line exactly
            # at the snap point to make the connection visible.
            if ok:
                preview_point = snap["point_canvas"]
        else:
            if self.snap_marker is not None:
                self.snap_marker.hide()

        if self.points:
            self._refresh(preview_point=preview_point)

    def canvasPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.button() == Qt.LeftButton:
            p = self.toMapCoordinates(event.pos())
            self.points.append(QgsPointXY(p))
            self._refresh()
            if self.message_callback:
                self.message_callback(f"Punti nuovo tracciato: {len(self.points)}. Clic destro per chiudere.")
        elif event.button() == Qt.RightButton:
            self.finish_line()

    def keyPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.key() == Qt.Key_Escape:
            self.clear_drawing()
            if self.message_callback:
                self.message_callback('New alignment drawing cancelled.')
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish_line()

    def _refresh(self, preview_point=None):
        if self.rubber is None:
            return
        self.rubber.reset(QgsWkbTypes.LineGeometry)
        pts = list(self.points)
        if preview_point is not None:
            pts.append(QgsPointXY(preview_point))
        for p in pts:
            self.rubber.addPoint(p, False)
        self.rubber.show()
        self.canvas.refresh()

    def clear_drawing(self):
        self.points = []
        if self.rubber:
            try:
                self.rubber.reset(QgsWkbTypes.LineGeometry)
                self.canvas.scene().removeItem(self.rubber)
            except Exception:
                pass
            self.rubber = None
        if getattr(self, "snap_line", None):
            try:
                self.snap_line.reset(QgsWkbTypes.LineGeometry)
                self.canvas.scene().removeItem(self.snap_line)
            except Exception:
                pass
            self.snap_line = None
        if getattr(self, "snap_marker", None):
            try:
                self.canvas.scene().removeItem(self.snap_marker)
            except Exception:
                pass
            self.snap_marker = None
        self.canvas.refresh()

    def finish_line(self):
        if len(self.points) < 2:
            if self.message_callback:
                self.message_callback('At least 2 points are required to create the new alignment.')
            return
        geom = QgsGeometry.fromPolylineXY(self.points)
        self.finished_callback(geom, self.canvas.mapSettings().destinationCrs())
        self.clear_drawing()
        if self.message_callback:
            self.message_callback('New alignment acquired and temporary sketch cleared.')


class PumpLinkDrawTool(QgsMapToolEmitPoint):
    """Interactive map tool used to draw a control link between existing nodes."""
    def __init__(self, canvas, node_layer, finished_callback, message_callback=None, snap_tolerance=2.0, link_label='pump', extra_nodes_provider=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.node_layer = node_layer
        self.finished_callback = finished_callback
        self.message_callback = message_callback
        self.snap_tolerance = float(snap_tolerance or 2.0)
        self.link_label = str(link_label or "link")
        self.extra_nodes_provider = extra_nodes_provider
        self.points = []
        self.points_layer = []
        self.nodes = []
        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.rubber.setColor(QColor(180, 0, 180, 200))
        self.rubber.setWidth(3)
        self.marker = QgsVertexMarker(canvas)
        self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.marker.setIconSize(22)
        self.marker.setPenWidth(4)
        self.marker.hide()

    def _transform_point(self, point, src_crs, dst_crs):
        if not src_crs or not dst_crs or src_crs == dst_crs:
            return QgsPointXY(point)
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        return QgsPointXY(tr.transform(QgsPointXY(point)))

    def _canvas_to_layer_point(self, point_canvas):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        return self._transform_point(point_canvas, canvas_crs, self.node_layer.crs())

    def _attr(self, f, names, default=None):
        for n in names:
            try:
                idx = f.fields().indexFromName(n)
                if idx >= 0:
                    v = f.attribute(idx)
                    if v not in [None, ""]:
                        return v
            except Exception:
                pass
        return default

    def _nearest_node(self, p_canvas):
        if self.node_layer is None:
            return None
        try:
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            p_layer = self._transform_point(p_canvas, canvas_crs, self.node_layer.crs())
            p_geom = QgsGeometry.fromPointXY(p_layer)
            best = None
            for f in self.node_layer.getFeatures():
                g = f.geometry()
                if not g or g.isEmpty():
                    continue
                node_pt = QgsPointXY(g.asMultiPoint()[0]) if g.isMultipart() else QgsPointXY(g.asPoint())
                d = QgsGeometry.fromPointXY(node_pt).distance(p_geom)
                if best is None or d < best["distance"]:
                    node_canvas = self._transform_point(node_pt, self.node_layer.crs(), canvas_crs)
                    node_id = self._attr(f, ["node_id", "NODE_ID", "id", "ID", "nome"], f.id())
                    best = {
                        "node_id": str(node_id),
                        "point_canvas": node_canvas,
                        "point_layer": node_pt,
                        "distance": float(d),
                    }
            # Also include manually created nodes/outfalls that have not yet
            # been added to the input point layer.
            try:
                extra_nodes = self.extra_nodes_provider() if callable(self.extra_nodes_provider) else []
            except Exception:
                extra_nodes = []
            for item in extra_nodes or []:
                try:
                    node_id = str(item.get("node_id") or item.get("id") or "")
                    if not node_id:
                        continue
                    node_pt = QgsPointXY(float(item.get("x")), float(item.get("y")))
                    src_crs = item.get("crs") or self.node_layer.crs()
                    node_canvas = self._transform_point(node_pt, src_crs, canvas_crs)
                    node_layer_pt = self._transform_point(node_pt, src_crs, self.node_layer.crs())
                    d = QgsGeometry.fromPointXY(node_layer_pt).distance(p_geom)
                    if best is None or d < best["distance"]:
                        best = {
                            "node_id": node_id,
                            "point_canvas": node_canvas,
                            "point_layer": node_layer_pt,
                            "distance": float(d),
                        }
                except Exception:
                    pass
            return best
        except Exception:
            return None

    def canvasMoveEvent(self, event):
        if not getattr(self, "_active", True):
            return
        p = QgsPointXY(self.toMapCoordinates(event.pos()))
        snap = self._nearest_node(p)
        if snap:
            ok = snap["distance"] <= self.snap_tolerance
            self.marker.setColor(QColor(0, 90, 255, 230) if ok else QColor(220, 0, 0, 220))
            self.marker.setCenter(snap["point_canvas"])
            self.marker.show()
            if self.points:
                self.rubber.reset(QgsWkbTypes.LineGeometry)
                for pt in self.points:
                    self.rubber.addPoint(pt, False)
                self.rubber.addPoint(snap["point_canvas"] if ok else p, True)
                self.rubber.show()
        elif self.marker:
            self.marker.hide()
            if self.points:
                self.rubber.reset(QgsWkbTypes.LineGeometry)
                for pt in self.points:
                    self.rubber.addPoint(pt, False)
                self.rubber.addPoint(p, True)
                self.rubber.show()

    def canvasPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.button() == Qt.LeftButton:
            p = QgsPointXY(self.toMapCoordinates(event.pos()))
            snap = self._nearest_node(p)
            if not self.nodes and (not snap or snap["distance"] > self.snap_tolerance):
                if self.message_callback:
                    self.message_callback(f"{self.link_label.capitalize()} non inserito: il primo punto deve essere un nodo esistente.")
                return

            if not self.nodes:
                self.nodes.append(snap)
                self.points.append(snap["point_canvas"])
                self.points_layer.append(snap["point_layer"])
                if self.message_callback:
                    self.message_callback(
                        f"Nodo di monte selezionato per {self.link_label}: {snap['node_id']}. "
                        'Now click any intermediate vertices, then click the downstream node.'
                    )
            elif snap and snap["distance"] <= self.snap_tolerance:
                if snap["node_id"] == self.nodes[0]["node_id"]:
                    if self.message_callback:
                        self.message_callback(f"{self.link_label.capitalize()} deve collegare due nodi distinti.")
                    return
                self.nodes.append(snap)
                self.points.append(snap["point_canvas"])
                self.points_layer.append(snap["point_layer"])
                self.finish()
                return
            else:
                self.points.append(p)
                self.points_layer.append(self._canvas_to_layer_point(p))
                if self.message_callback:
                    self.message_callback(f"Vertice intermedio {self.link_label} aggiunto ({len(self.points) - 1}). Clicca il nodo di valle per chiudere.")

            self.rubber.reset(QgsWkbTypes.LineGeometry)
            for pt in self.points:
                self.rubber.addPoint(pt, False)
            self.rubber.show()
        elif event.button() == Qt.RightButton:
            self.clear_drawing()

    def keyPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.key() == Qt.Key_Escape:
            self.clear_drawing()

    def finish(self):
        self.finished_callback(self.nodes, self.points_layer)
        self.clear_drawing()

    def clear_drawing(self):
        self.points = []
        self.points_layer = []
        self.nodes = []
        if self.rubber:
            try:
                self.rubber.reset(QgsWkbTypes.LineGeometry)
                self.canvas.scene().removeItem(self.rubber)
            except Exception:
                pass
            self.rubber = None
        if self.marker:
            try:
                self.canvas.scene().removeItem(self.marker)
            except Exception:
                pass
            self.marker = None
        self.canvas.refresh()


class ManualSWMMNodeDrawTool(QgsMapToolEmitPoint):
    """Interactive map tool used to add a manual SWMM node or outfall at the model outlet."""
    def __init__(self, canvas, target_crs, finished_callback, message_callback=None, label='node'):
        super().__init__(canvas)
        self.canvas = canvas
        self.target_crs = target_crs
        self.finished_callback = finished_callback
        self.message_callback = message_callback
        self.label = str(label or 'node')
        self.marker = QgsVertexMarker(canvas)
        self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.marker.setIconSize(18)
        self.marker.setPenWidth(3)
        self.marker.setColor(QColor(0, 160, 90, 230))
        self.marker.hide()

    def _transform_point(self, point, src_crs, dst_crs):
        if not src_crs or not dst_crs or src_crs == dst_crs:
            return QgsPointXY(point)
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        return QgsPointXY(tr.transform(QgsPointXY(point)))

    def canvasMoveEvent(self, event):
        if not getattr(self, "_active", True):
            return
        p = QgsPointXY(self.toMapCoordinates(event.pos()))
        self.marker.setCenter(p)
        self.marker.show()

    def canvasPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.button() == Qt.LeftButton:
            p_canvas = QgsPointXY(self.toMapCoordinates(event.pos()))
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            p_target = self._transform_point(p_canvas, canvas_crs, self.target_crs)
            self.finished_callback(p_target, self.target_crs)
            self.clear_drawing()
            if self.message_callback:
                self.message_callback(f"{self.label.capitalize()} manuale acquisito sulla mappa.")
        elif event.button() == Qt.RightButton:
            self.clear_drawing()

    def keyPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.key() == Qt.Key_Escape:
            self.clear_drawing()

    def clear_drawing(self):
        if self.marker:
            try:
                self.canvas.scene().removeItem(self.marker)
            except Exception:
                pass
            self.marker = None
        self.canvas.refresh()


class ManualProfileNodeTool(QgsMapToolEmitPoint):
    """Interactive map tool used to add a manual node in the profile editor.

    The point is visually snapped to the reference alignment selected in the
    profile editor. The node is created only when the click falls within the
    snap tolerance, keeping chainage and distance consistent with the polyline.
    """
    def __init__(self, canvas, finished_callback, message_callback=None, previous_tool=None, snap_layer=None, snap_tolerance=2.0):
        super().__init__(canvas)
        self.canvas = canvas
        self.finished_callback = finished_callback
        self.message_callback = message_callback
        self.previous_tool = previous_tool
        self.snap_layer = snap_layer
        self.snap_tolerance = float(snap_tolerance or 2.0)
        self.last_snap = None
        self._active = True

        self.marker = QgsVertexMarker(canvas)
        self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.marker.setIconSize(22)
        self.marker.setPenWidth(4)
        self.marker.setColor(QColor(0, 150, 255, 230))
        self.marker.hide()

        self.snap_line = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.snap_line.setColor(QColor(0, 150, 255, 180))
        self.snap_line.setWidth(2)

    def _ensure_canvas_items(self):
        """Recreate marker and RubberBand items after a clear operation.

        QGIS may still emit a canvasMoveEvent after the marker has been removed
        from the scene during a map tool switch. Without this guard, the tool
        can raise AttributeError: 'NoneType' object has no attribute 'setColor'.
        """
        if not getattr(self, "_active", True):
            return
        if getattr(self, "marker", None) is None:
            self.marker = QgsVertexMarker(self.canvas)
            self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            self.marker.setIconSize(22)
            self.marker.setPenWidth(4)
            self.marker.setColor(QColor(0, 150, 255, 230))
            self.marker.hide()
        if getattr(self, "snap_line", None) is None:
            self.snap_line = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self.snap_line.setColor(QColor(0, 150, 255, 180))
            self.snap_line.setWidth(2)

    def _safe_message(self, text):
        """Send a status message only if the callback target is still valid."""
        if not self.message_callback:
            return
        try:
            self.message_callback(text)
        except RuntimeError:
            # The profile editor/status QLabel may have already been destroyed
            # while QGIS is still dispatching map canvas events. Ignore the
            # stale callback to avoid non-critical warnings.
            pass

    def _transform_point(self, point, src_crs, dst_crs):
        if not src_crs or not dst_crs or src_crs == dst_crs:
            return QgsPointXY(point)
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        return QgsPointXY(tr.transform(QgsPointXY(point)))

    def _candidate_line_geometries(self):
        """Return candidate line geometries for snapping.

        If the layer contains selected features, use them; otherwise, use all
        layer features. This keeps snapping visible even when the alignment is
        made of multiple elements.
        """
        if self.snap_layer is None:
            return []
        feats = []
        try:
            selected = list(self.snap_layer.selectedFeatures())
            feats = selected if selected else list(self.snap_layer.getFeatures())
        except Exception:
            feats = []
        geoms = []
        for f in feats:
            try:
                g = f.geometry()
                if g and not g.isEmpty():
                    geoms.append(g)
            except Exception:
                pass
        return geoms

    def _nearest_snap_point(self, p_canvas):
        """Return ``{'point_canvas': QgsPointXY, 'distance': float}`` on the alignment."""
        try:
            if self.snap_layer is None:
                return None
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            p_layer = self._transform_point(p_canvas, canvas_crs, self.snap_layer.crs())
            p_geom = QgsGeometry.fromPointXY(p_layer)
            best = None
            for line_geom in self._candidate_line_geometries():
                try:
                    near = line_geom.nearestPoint(p_geom)
                    if not near or near.isEmpty():
                        continue
                    d = float(near.distance(p_geom))
                    if best is None or d < best["distance"]:
                        near_pt_layer = QgsPointXY(near.asPoint())
                        near_pt_canvas = self._transform_point(near_pt_layer, self.snap_layer.crs(), canvas_crs)
                        best = {"point_canvas": near_pt_canvas, "distance": d}
                except Exception:
                    continue
            # Also include manually created nodes/outfalls that have not yet
            # been added to the input point layer.
            try:
                extra_nodes = self.extra_nodes_provider() if callable(self.extra_nodes_provider) else []
            except Exception:
                extra_nodes = []
            for item in extra_nodes or []:
                try:
                    node_id = str(item.get("node_id") or item.get("id") or "")
                    if not node_id:
                        continue
                    node_pt = QgsPointXY(float(item.get("x")), float(item.get("y")))
                    src_crs = item.get("crs") or self.node_layer.crs()
                    node_canvas = self._transform_point(node_pt, src_crs, canvas_crs)
                    node_layer_pt = self._transform_point(node_pt, src_crs, self.node_layer.crs())
                    d = QgsGeometry.fromPointXY(node_layer_pt).distance(p_geom)
                    if best is None or d < best["distance"]:
                        best = {
                            "node_id": node_id,
                            "point_canvas": node_canvas,
                            "point_layer": node_layer_pt,
                            "distance": float(d),
                        }
                except Exception:
                    pass
            return best
        except Exception:
            return None

    def canvasMoveEvent(self, event):
        if not getattr(self, "_active", True):
            return
        p = QgsPointXY(self.toMapCoordinates(event.pos()))
        snap = self._nearest_snap_point(p)
        self.last_snap = snap

        self._ensure_canvas_items()

        if getattr(self, "snap_line", None) is not None:
            self.snap_line.reset(QgsWkbTypes.LineGeometry)

        if snap:
            ok = snap["distance"] <= self.snap_tolerance
            color = QColor(0, 150, 255, 230) if ok else QColor(220, 0, 0, 220)
            if getattr(self, "marker", None) is None:
                return
            self.marker.setColor(color)
            self.marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            self.marker.setIconSize(24 if ok else 20)
            self.marker.setCenter(snap["point_canvas"])
            self.marker.show()

            if getattr(self, "snap_line", None) is not None:
                self.snap_line.setColor(color)
                self.snap_line.addPoint(p, False)
                self.snap_line.addPoint(snap["point_canvas"], True)
                self.snap_line.show()

            if ok:
                self._safe_message(f"Valid snap on alignment - distance {snap['distance']:.2f} m. Left-click to insert the node.")
            else:
                self._safe_message(f"Outside snap tolerance ({snap['distance']:.2f} m > {self.snap_tolerance:.2f} m). Move closer to the alignment.")
        else:
            if getattr(self, "marker", None) is not None:
                self.marker.hide()
            self._safe_message('No valid alignment available for manual node snapping.')
        self.canvas.refresh()

    def canvasPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.button() == Qt.LeftButton:
            p = QgsPointXY(self.toMapCoordinates(event.pos()))
            snap = self._nearest_snap_point(p)
            self.last_snap = snap
            if not snap:
                self._safe_message("Node not inserted: no snap is available. Select the reference alignment in the profile editor.")
                return
            if snap["distance"] > self.snap_tolerance:
                self._safe_message(f"Node not inserted: outside snap tolerance ({snap['distance']:.2f} m > {self.snap_tolerance:.2f} m).")
                return
            self.finished_callback(snap["point_canvas"], self.canvas.mapSettings().destinationCrs())
            self.clear_drawing(restore_tool=True)
            self._safe_message('Manual node acquired and snapped to the alignment.')
        elif event.button() == Qt.RightButton:
            self.clear_drawing(restore_tool=True)
            self._safe_message('Manual node insertion cancelled.')

    def keyPressEvent(self, event):
        if not getattr(self, "_active", True):
            return
        if event.key() == Qt.Key_Escape:
            self.clear_drawing(restore_tool=True)
            self._safe_message('Manual node insertion cancelled.')

    def clear_drawing(self, restore_tool=False):
        self._active = False
        self.message_callback = None
        if getattr(self, "marker", None):
            try:
                self.canvas.scene().removeItem(self.marker)
            except Exception:
                pass
            self.marker = None
        if getattr(self, "snap_line", None):
            try:
                self.snap_line.reset(QgsWkbTypes.LineGeometry)
                self.canvas.scene().removeItem(self.snap_line)
            except Exception:
                pass
            self.snap_line = None
        if restore_tool and getattr(self, "previous_tool", None) is not None:
            try:
                if self.canvas.mapTool() is self:
                    self.canvas.setMapTool(self.previous_tool)
            except Exception:
                pass
        try:
            if self.canvas.mapTool() is self:
                self.canvas.unsetMapTool(self)
        except Exception:
            pass
        self.canvas.refresh()

    def deactivate_tool(self, restore_tool=True):
        """Fully disable this temporary map tool and remove canvas overlays."""
        self.clear_drawing(restore_tool=restore_tool)


# =========================================================
# PROFILE EDITOR - PyQt version embedded in QGIS
# =========================================================
try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    MATPLOTLIB_OK = True
except Exception:
    FigureCanvas = None
    Figure = None
    MATPLOTLIB_OK = False


class CurvePreviewDialog(QDialog):
    """Simple dialog used to preview a user-defined tabular curve."""
    def __init__(self, parent, title, x_label, y_label, points):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 520)
        layout = QVBoxLayout(self)

        if not MATPLOTLIB_OK:
            msg = QLabel('Matplotlib is not available in the QGIS Python environment: the chart cannot be displayed.')
            msg.setWordWrap(True)
            layout.addWidget(msg)
        else:
            fig = Figure(figsize=(7.5, 4.8), dpi=100)
            ax = fig.add_subplot(111)
            pts = sorted([(float(x), float(y)) for x, y in points], key=lambda p: p[0])
            xs = [x for x, _y in pts]
            ys = [y for _x, y in pts]
            ax.plot(xs, ys, marker="o", linewidth=1.8)
            ax.set_title(title)
            ax.set_xlabel(x_label)
            ax.set_ylabel(y_label)
            ax.grid(True, alpha=0.30)
            for x, y in pts:
                ax.annotate(f"{x:g}; {y:g}", (x, y), textcoords="offset points", xytext=(5, 5), fontsize=8)
            fig.tight_layout()
            canvas = FigureCanvas(fig)
            layout.addWidget(canvas)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


MATERIALI_DIAMETRI = {
    "PVC": [160, 200, 250, 315, 400, 500, 630, 800],
    "CLS": [300, 400, 500, 600, 800, 1200, 1500, 1800, 2000],
    "GRES": [150, 200, 250, 300, 350, 400, 500, 600],
    "Polietilene strutturato AD": [170, 218, 273, 300, 344, 400, 427, 500, 533, 600, 691, 800, 855, 1024],
}

COEFF_K_MATERIALE = {
    "PVC": 100.0,
    "CLS": 80.0,
    "GRES": 80.0,
    "Polietilene strutturato AD": 80.0,
}

COSTO_SCAVO = {
    "A": [
        (0.0, 1.5, 0, 315, 35.11, "E.3.06.01"), (0.0, 1.5, 315, 630, 43.31, "E.3.06.05"),
        (1.5, 2.0, 0, 350, 65.38, "E.3.06.21"), (1.5, 2.0, 350, 630, 73.02, "E.3.06.15"),
        (2.0, 2.5, 0, 350, 76.47, "E.3.06.21"), (2.0, 2.5, 350, 630, 97.83, "E.3.06.25"),
        (2.5, 3.0, 0, 350, 87.60, "E.3.06.31"), (2.5, 3.0, 350, 630, 111.44, "E.3.06.35"),
        (3.0, 3.5, 0, 350, 109.89, "E.3.06.41"), (3.0, 3.5, 350, 630, 128.69, "E.3.06.45"),
        (3.5, 4.0, 0, 350, 122.31, "E.3.06.51"), (3.5, 4.0, 350, 630, 142.78, "E.3.06.55"),
        (4.0, 4.5, 0, 350, 144.10, "E.3.06.61"), (4.0, 4.5, 350, 630, 166.21, "E.3.06.65"),
    ],
    "B": [
        (0.0, 1.5, 0, 315, 81.89, "E.3.06.02"), (0.0, 1.5, 315, 630, 86.61, "E.3.06.06"),
        (1.5, 2.0, 0, 350, 144.61, "E.3.06.12"), (1.5, 2.0, 350, 630, 153.54, "E.3.06.16"),
        (2.0, 2.5, 0, 350, 183.52, "E.3.06.22"), (2.0, 2.5, 350, 630, 228.29, "E.3.06.26"),
        (2.5, 3.0, 0, 350, 222.47, "E.3.06.32"), (2.5, 3.0, 350, 630, 277.80, "E.3.06.36"),
        (3.0, 3.5, 0, 350, 288.22, "E.3.06.42"), (3.0, 3.5, 350, 630, 330.94, "E.3.06.46"),
        (3.5, 4.0, 0, 350, 331.12, "E.3.06.52"), (3.5, 4.0, 350, 630, 380.91, "E.3.06.56"),
        (4.0, 4.5, 0, 350, 396.52, "E.3.06.62"), (4.0, 4.5, 350, 630, 453.21, "E.3.06.66"),
    ],
    "C": [
        (0.0, 1.5, 0, 315, 115.16, "E.3.06.03"), (0.0, 1.5, 315, 630, 123.47, "E.3.06.07"),
        (1.5, 2.0, 0, 350, 181.17, "E.3.06.13"), (1.5, 2.0, 350, 630, 193.73, "E.3.06.17"),
        (2.0, 2.5, 0, 350, 220.09, "E.3.06.23"), (2.0, 2.5, 350, 630, 275.72, "E.3.06.27"),
        (2.5, 3.0, 0, 350, 259.03, "E.3.06.33"), (2.5, 3.0, 350, 630, 325.25, "E.3.06.37"),
        (3.0, 3.5, 0, 350, 328.42, "E.3.06.43"), (3.0, 3.5, 350, 630, 378.38, "E.3.06.47"),
        (3.5, 4.0, 0, 350, 371.33, "E.3.06.53"), (3.5, 4.0, 350, 630, 428.33, "E.3.06.57"),
        (4.0, 4.5, 0, 350, 439.14, "E.3.06.53"), (4.0, 4.5, 350, 630, 503.21, "E.3.06.67"),
    ],
}

COSTO_CONDOTTA = {
    "CLS": [(300, 66.17, "G.1.12.03"), (400, 79.35, "G.1.12.04"), (500, 99.27, "G.1.12.05"), (600, 124.05, "G.1.12.06"), (800, 276.12, "G.1.12.08"), (1000, 314.07, "G.1.12.10"), (1200, 462.08, "G.1.12.12"), (1400, 628.13, "G.1.12.14")],
    "PVC": [(160, 27.13, "G.1.13.90"), (200, 39.69, "G.1.13.91"), (250, 57.56, "G.1.13.92"), (315, 88.43, "G.1.13.93"), (400, 139.82, "G.1.13.94"), (500, 286.13, "G.1.13.95"), (630, 340.90, "G.1.13.96"), (800, 702.95, "G.1.13.97")],
    "GRES": [(150, 32.18, "G.1.11.100"), (200, 50.66, "G.1.11.101"), (250, 75.96, "G.1.11.102"), (300, 98.78, "G.1.11.103"), (350, 141.75, "G.1.11.104"), (400, 162.82, "G.1.11.105"), (500, 215.84, "G.1.11.106"), (630, 284.12, "G.1.11.107")],
}


def _to_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "."))
    except Exception:
        return default


def _geom_circolare(D, h_rel):
    r = D / 2.0
    if h_rel <= 0:
        return 0.0, 0.0, 0.0
    if h_rel >= 1:
        A = math.pi * r ** 2
        P = 2.0 * math.pi * r
        return A, P, A / P if P > 0 else 0.0
    h = h_rel * D
    theta = 2.0 * math.acos((r - h) / r)
    A = 0.5 * r ** 2 * (theta - math.sin(theta))
    P = r * theta
    return A, P, A / P if P > 0 else 0.0


def _portata_circolare(D, K, pendenza, h_rel):
    A, P, R = _geom_circolare(D, h_rel)
    if A <= 0 or R <= 0 or pendenza <= 0 or K <= 0:
        return 0.0
    return K * A * (R ** (2.0 / 3.0)) * math.sqrt(pendenza)


def _trova_tirante_reale(D, K, pendenza, Q, tol=1e-8, max_iter=100):
    if D <= 0 or K <= 0 or pendenza <= 0 or Q <= 0:
        return None, None, False
    q_full = _portata_circolare(D, K, pendenza, 1.0)
    if q_full <= 0 or Q > q_full:
        return None, q_full, False
    lo, hi = 1e-6, 1.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        q_mid = _portata_circolare(D, K, pendenza, mid)
        if abs(q_mid - Q) < tol:
            return mid, q_mid, True
        if q_mid < Q:
            lo = mid
        else:
            hi = mid
    mid = 0.5 * (lo + hi)
    q_mid = _portata_circolare(D, K, pendenza, mid)
    return mid, q_mid, True


def _costo_scavo_stratificato(tipo_raw, prof_valle, prof_monte, lunghezza, diam_m):
    tipo = str(tipo_raw or "").strip().upper()
    if tipo not in COSTO_SCAVO:
        return None, "", ""
    try:
        prof_valle = float(prof_valle); prof_monte = float(prof_monte); lunghezza = float(lunghezza); diam_m = float(diam_m)
    except Exception:
        return None, "", ""
    if lunghezza <= 0 or diam_m <= 0:
        return None, "", ""
    diam_mm = diam_m * 1000.0
    fasce = [(a, b, c, cod) for a, b, dmin, dmax, c, cod in COSTO_SCAVO[tipo] if dmin <= diam_mm <= dmax]
    if not fasce:
        return None, "", ""
    fasce.sort(key=lambda x: x[0])
    p0, p1 = min(prof_valle, prof_monte), max(prof_valle, prof_monte)
    if abs(p1 - p0) < 1e-9:
        for a, b, costo_ml, cod in fasce:
            if a <= prof_valle <= b:
                tot = lunghezza * costo_ml
                return round(tot, 2), cod, f"FASCIA {a:.2f}-{b:.2f} m | L={lunghezza:.2f} m | {cod} | {lunghezza:.2f} x {costo_ml:.2f} = {tot:.2f} EUR"
        return None, "", ""
    grad = (p1 - p0) / lunghezza
    tot = 0.0; codici = []; dettagli = []
    for a, b, costo_ml, cod in fasce:
        h0, h1 = max(p0, a), min(p1, b)
        if h1 <= h0:
            continue
        li = (h1 - h0) / grad
        parz = li * costo_ml
        tot += parz; codici.append(cod)
        dettagli.append(f"FASCIA {a:.2f}-{b:.2f} m | sviluppo={li:.2f} m | {cod} | {li:.2f} x {costo_ml:.2f} = {parz:.2f} EUR")
    if tot <= 0:
        return None, "", ""
    return round(tot, 2), " + ".join(codici), " || ".join(dettagli) + f" || TOTALE={tot:.2f} EUR"


def _costo_posa(materiale, diam_m):
    mat = str(materiale or "").strip().upper()
    if mat not in COSTO_CONDOTTA:
        return None, ""
    try:
        dn = int(round(float(diam_m) * 1000.0))
    except Exception:
        return None, ""
    for d, costo, cod in COSTO_CONDOTTA[mat]:
        if d == dn:
            return costo, cod
    return None, ""


class ProfileEditorDialog(QDialog):
    """Integrated sewer longitudinal profile editor for the QGIS plugin.

    Implements the main workflow of the desktop Sewer Builder application:
    downstream-to-upstream computation, invert drops, slope edits, diameter
    upgrades, undo handling, hydraulic checks, profile table, and plot.
    """
    def __init__(self, parent_builder, point_layer, outdir, parent=None, allow_manual_nodes=True):
        super().__init__(parent)
        self.builder = parent_builder
        self.point_layer = point_layer
        self.outdir = outdir or os.path.expanduser("~")
        self.rows = []
        self.undo_stack = []
        self.output_layer = None
        self.allow_manual_nodes = True  # Keep manual GIS node insertion available in all profile editor workflows.
        self.setWindowTitle('Longitudinal Profile Editor - Sewer Builder QGIS')
        self.resize(1450, 850)
        self._load_rows_from_layer()
        self._build_ui()
        # If the manhole layer already contains the connection invert at the downstream node
        # (for example, a proposed sewer snapped to an existing conduit), use it as Hi.
        try:
            if self.rows and self.rows[-1].get("invert_elevation") is not None:
                self.txt_hi_prof.setText(f"{float(self.rows[-1]['invert_elevation']):.3f}")
        except Exception:
            pass
        self._refresh_all()

    def _attr(self, f, names, default=None):
        fs = f.fields().names()
        for n in names:
            if n in fs and f[n] not in [None, ""]:
                return f[n]
        return default

    def _attr_float(self, f, names, default=None):
        return _to_float(self._attr(f, names, None), default)

    def _load_rows_from_layer(self):
        tmp = []
        for i, f in enumerate(self.point_layer.getFeatures()):
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            pk = self._attr_float(f, ["pk", "PK"], None)
            if pk is None:
                pk = float(i)
            dist = self._attr_float(f, ["Distance", "distanza", "DISTANZA"], 0.0)
            elev = self._attr_float(f, ["ground_elevation", "elevaz", "quota_terr", "ELEVATION"], 0.0)
            nid = self._attr(f, ["node_id", "NODE_ID", "Id", "ID", "id"], str(i + 1))
            tmp.append({
                "geometry": QgsGeometry(g), "node_id": str(nid), "pk": float(pk), "Distance": float(dist or 0.0),
                "ground_elevation": float(elev or 0.0), "invert_elevation": self._attr_float(f, ["invert_elevation", "q_scorr", "quota_fondo"], None),
                "excavation_depth": self._attr_float(f, ["excavation_depth", "prof_scav"], None), "pendenza": self._attr_float(f, ["pendenza", "Slope"], None),
                "D": self._attr_float(f, ["D", "diam_m", "diametro"], None), "Q": self._attr_float(f, ["Q", "Q_m3s"], None),
                "K": self._attr_float(f, ["K"], None), "materiale": str(self._attr(f, ["materiale", "MATERIALE"], "")),
                "tipo_scavo": str(self._attr(f, ["tipo_scavo", "tipo"], "A")), "salto_fondo": self._attr_float(f, ["salto_fondo", "salto_f"], 0.0),
                "larghezza_scavo": self._attr_float(f, ["larghezza_scavo", "largh_sc"], 1.0),
                "quota_fissa": bool(int(_to_float(self._attr(f, ["quota_fissa", "q_fissa", "fixed_q"], 0), 0) or 0)),
                "nodo_manuale": bool(int(_to_float(self._attr(f, ["nodo_man", "manuale", "manual_node"], 0), 0) or 0)),
            })
        tmp.sort(key=lambda r: r["pk"])
        # Recompute Distanza from the difference between chainages if the field is not valid.
        prev = None
        for r in tmp:
            if prev is None:
                r["Distance"] = 0.0
            elif not r.get("Distance") or r.get("Distance") < 0:
                r["Distance"] = max(0.0, r["pk"] - prev)
            prev = r["pk"]
        self.rows = tmp

    def _build_ui(self):
        main = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        main.addWidget(splitter)

        left_scroll = QScrollArea(); left_scroll.setWidgetResizable(True); left_scroll.setMinimumWidth(390)
        left = QGroupBox('Profile commands')
        left_layout = QVBoxLayout(left)
        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        box1 = QGroupBox('1) Initial data')
        f1 = QFormLayout(box1)
        self.spn_q = QDoubleSpinBox(); self.spn_q.setRange(0.0, 1000000.0); self.spn_q.setDecimals(3); self.spn_q.setValue(10.0); self.spn_q.setSuffix(" l/s")
        self.txt_hi_prof = QLineEdit(""); self.txt_hi_prof.setPlaceholderText("if empty: last ground elevation - 1 m")
        self.spn_pendenza = QDoubleSpinBox(); self.spn_pendenza.setRange(0.00001, 1.0); self.spn_pendenza.setDecimals(5); self.spn_pendenza.setValue(0.005)
        self.spn_larghezza = QDoubleSpinBox(); self.spn_larghezza.setRange(0.1, 20.0); self.spn_larghezza.setDecimals(2); self.spn_larghezza.setValue(1.0)
        self.cmb_materiale = QComboBox(); self.cmb_materiale.addItems(list(MATERIALI_DIAMETRI.keys())); self.cmb_materiale.currentTextChanged.connect(self._update_dn_lists)
        self.cmb_dn = QComboBox(); self.cmb_tipo_scavo = QComboBox(); self.cmb_tipo_scavo.addItems(list(COSTO_SCAVO.keys()))
        f1.addRow('Flow Q', self.spn_q); f1.addRow('Downstream invert Hi [m]', self.txt_hi_prof); f1.addRow('Slope [m/m]', self.spn_pendenza)
        f1.addRow('Excavation width [m]', self.spn_larghezza); f1.addRow('Material', self.cmb_materiale); f1.addRow('DN diameter [mm]', self.cmb_dn); f1.addRow('Excavation type', self.cmb_tipo_scavo)
        btn_calc = QPushButton('Compute / update profile'); btn_calc.clicked.connect(self.calculate_initial_profile); f1.addRow(btn_calc)
        btn_ver = QPushButton('Run 80% filling + velocity checks'); btn_ver.clicked.connect(self.run_checks); f1.addRow(btn_ver)
        self.btn_undo = QPushButton('↶ Undo last change'); self.btn_undo.clicked.connect(self.undo); f1.addRow(self.btn_undo)
        left_layout.addWidget(box1)

        box2 = QGroupBox('2) Dynamic edits')
        g = QGridLayout(box2)
        self.txt_drop_node = QLineEdit(); self.spn_drop = QDoubleSpinBox(); self.spn_drop.setRange(-10.0, 10.0); self.spn_drop.setDecimals(3); self.spn_drop.setValue(0.20)
        self.txt_slope_node = QLineEdit(); self.spn_new_slope = QDoubleSpinBox(); self.spn_new_slope.setRange(0.00001, 1.0); self.spn_new_slope.setDecimals(5); self.spn_new_slope.setValue(0.005)
        self.txt_diam_node = QLineEdit(); self.cmb_new_dn = QComboBox(); self.spn_new_width = QDoubleSpinBox(); self.spn_new_width.setRange(0.1, 20.0); self.spn_new_width.setDecimals(2); self.spn_new_width.setValue(1.0)
        g.addWidget(QLabel('Invert drop after node_id'), 0, 0); g.addWidget(self.txt_drop_node, 0, 1); g.addWidget(QLabel('Drop [m]'), 1, 0); g.addWidget(self.spn_drop, 1, 1)
        b_drop = QPushButton('Insert invert drop'); b_drop.clicked.connect(self.insert_drop); g.addWidget(b_drop, 2, 0, 1, 2)
        g.addWidget(QLabel('Slope up to node_id'), 3, 0); g.addWidget(self.txt_slope_node, 3, 1); g.addWidget(QLabel('New slope'), 4, 0); g.addWidget(self.spn_new_slope, 4, 1)
        b_sl = QPushButton('Edit slope'); b_sl.clicked.connect(self.modify_slope); g.addWidget(b_sl, 5, 0, 1, 2)
        g.addWidget(QLabel('Diameter from node_id'), 6, 0); g.addWidget(self.txt_diam_node, 6, 1); g.addWidget(QLabel('New DN [mm]'), 7, 0); g.addWidget(self.cmb_new_dn, 7, 1)
        g.addWidget(QLabel('New width [m]'), 8, 0); g.addWidget(self.spn_new_width, 8, 1)
        b_dn = QPushButton('Increase diameter'); b_dn.clicked.connect(self.increase_diameter); g.addWidget(b_dn, 9, 0, 1, 2)

        box_manual = QGroupBox('Manual node insertion from GIS')
        gm = QGridLayout(box_manual)
        self.cmb_manual_trace = QComboBox()
        self.cmb_manual_trace.setToolTip(
            'Linear alignment used to snap the manual node. '
            'Select the correct pipe before clicking on the map.'
        )
        self.btn_refresh_manual_trace = QPushButton('Refresh alignments')
        self.btn_refresh_manual_trace.clicked.connect(self._populate_manual_trace_combo)
        self.txt_manual_node_id = QLineEdit()
        self.txt_manual_node_id.setPlaceholderText("auto: M1, M2...")
        self.cmb_manual_node_mode = QComboBox()
        self.cmb_manual_node_mode.addItems(['Variable invert from profile', 'Fixed invert elevation'])
        self.spn_manual_fixed_q = QDoubleSpinBox()
        self.spn_manual_fixed_q.setRange(-10000.0, 10000.0)
        self.spn_manual_fixed_q.setDecimals(3)
        self.spn_manual_fixed_q.setValue(0.0)
        self.spn_manual_fixed_q.setToolTip('Manual node invert elevation, used only in fixed-elevation mode.')
        self.btn_add_manual_node = QPushButton('Add node on map')
        self.btn_add_manual_node.clicked.connect(self.start_add_manual_node)
        gm.addWidget(QLabel('Reference alignment'), 0, 0); gm.addWidget(self.cmb_manual_trace, 0, 1)
        gm.addWidget(self.btn_refresh_manual_trace, 1, 0, 1, 2)
        gm.addWidget(QLabel('New node_id'), 2, 0); gm.addWidget(self.txt_manual_node_id, 2, 1)
        gm.addWidget(QLabel('Elevation type'), 3, 0); gm.addWidget(self.cmb_manual_node_mode, 3, 1)
        gm.addWidget(QLabel('Fixed invert elevation [m]'), 4, 0); gm.addWidget(self.spn_manual_fixed_q, 4, 1)
        gm.addWidget(self.btn_add_manual_node, 5, 0, 1, 2)
        if self.allow_manual_nodes:
            self._populate_manual_trace_combo()
            g.addWidget(box_manual, 10, 0, 1, 2)
        else:
            # Manual nodes are disabled when correcting a profile after conduits have
            # already been created. The profile could be saved, but the existing
            # geometries and segments would not be regenerated from the new nodes.
            # Hide the panel to avoid inconsistent network data.
            self.cmb_manual_trace = None
            self.btn_add_manual_node = None
        left_layout.addWidget(box2)

        box3 = QGroupBox("3) Output")
        v3 = QVBoxLayout(box3)
        b_save = QPushButton('Save profile GPKG + CSV + DXF'); b_save.clicked.connect(self.save_outputs); v3.addWidget(b_save)
        b_close = QPushButton('Close editor'); b_close.clicked.connect(self.accept); v3.addWidget(b_close)
        left_layout.addWidget(box3); left_layout.addStretch(1)

        right = QSplitter(Qt.Vertical)
        splitter.addWidget(right); splitter.setStretchFactor(1, 1)
        plot_box = QGroupBox('Real-time updated profile')
        p_layout = QVBoxLayout(plot_box)
        if MATPLOTLIB_OK:
            self.fig = Figure(figsize=(9, 5), dpi=100); self.ax = self.fig.add_subplot(111); self.canvas_plot = FigureCanvas(self.fig); p_layout.addWidget(self.canvas_plot)
        else:
            self.fig = self.ax = self.canvas_plot = None
            p_layout.addWidget(QLabel('Matplotlib is not available in the QGIS Python environment: the profile can still be saved and viewed in the table.'))
        right.addWidget(plot_box)

        table_box = QGroupBox('Calculation table')
        t_layout = QVBoxLayout(table_box)
        self.table_cols = ["node_id", "pk", "Distance", "ground_elevation", "invert_elevation", "excavation_depth", "quota_fissa", "nodo_manuale", "pendenza", "D", "Q", "Q_max_GR80", "GR", "depth", "velocity", "ok_GR80", "OK_velocity", "salto_fondo", "excavation_cost", "pipe_cost_tot"]
        self.table = QTableWidget(0, len(self.table_cols)); self.table.setHorizontalHeaderLabels(self.table_cols); self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        t_layout.addWidget(self.table); right.addWidget(table_box)
        self.status = QLabel('Editor ready.'); main.addWidget(self.status)
        self._update_dn_lists()
        self._update_undo_button()

    def _update_dn_lists(self):
        mat = self.cmb_materiale.currentText() if hasattr(self, "cmb_materiale") else "PVC"
        vals = [str(d) for d in MATERIALI_DIAMETRI.get(mat, [])]
        cur = self.cmb_dn.currentText() if hasattr(self, "cmb_dn") else ""
        self.cmb_dn.clear(); self.cmb_dn.addItems(vals)
        self.cmb_new_dn.clear(); self.cmb_new_dn.addItems(vals)
        if cur and cur in vals:
            self.cmb_dn.setCurrentText(cur)
        if vals:
            self.cmb_new_dn.setCurrentIndex(min(1, len(vals) - 1))

    def _push_undo(self, label):
        self.undo_stack.append([dict(r) for r in self.rows])
        if len(self.undo_stack) > 20:
            self.undo_stack.pop(0)
        self._update_undo_button(); self.status.setText(f"Stato salvato per undo: {label}")

    def _update_undo_button(self):
        if hasattr(self, "btn_undo"):
            self.btn_undo.setEnabled(bool(self.undo_stack))

    def undo(self):
        if not self.undo_stack:
            return
        self.rows = self.undo_stack.pop()
        self._refresh_all(); self._update_undo_button(); self.status.setText("Undo eseguito.")

    def _idx_for_node(self, node_text):
        raw = str(node_text).strip()
        if not raw:
            raise ValueError('Enter a node_id.')
        for i, r in enumerate(self.rows):
            if str(r["node_id"]) == raw:
                return i
        # Support numeric node_id values entered without decimal places.
        for i, r in enumerate(self.rows):
            try:
                if int(float(r["node_id"])) == int(float(raw)):
                    return i
            except Exception:
                pass
        raise ValueError(f"Nessuna riga trovata con node_id = {raw}")

    def _recalculate(self):
        if len(self.rows) < 2:
            raise ValueError('At least 2 nodes are required.')
        # Keep chainage and segment distance values consistent.
        self.rows.sort(key=lambda r: (float(r.get("pk", 0.0)), str(r.get("node_id", ""))))
        for i, r in enumerate(self.rows):
            r["Distance"] = 0.0 if i == 0 else max(0.0, float(r["pk"]) - float(self.rows[i - 1]["pk"]))
            r["salto_fondo"] = _to_float(r.get("salto_fondo"), 0.0) or 0.0
            r["pendenza"] = _to_float(r.get("pendenza"), self.spn_pendenza.value()) or self.spn_pendenza.value()
            r["quota_fissa"] = bool(r.get("quota_fissa", False))
            r["nodo_manuale"] = bool(r.get("nodo_manuale", False))

        # The downstream node remains the final constraint, unless fixed intermediate inverts exist.
        if self.rows[-1].get("invert_elevation") is None:
            last_ground = _to_float(self.rows[-1].get("ground_elevation"), 0.0) or 0.0
            self.rows[-1]["invert_elevation"] = last_ground - 1.0

        # Downstream-to-upstream pass: fixed-invert nodes are not overwritten.
        # Instead, each fixed node becomes a new elevation constraint for upstream nodes.
        for i in range(len(self.rows) - 2, -1, -1):
            for key in ["K", "D", "Q", "materiale", "tipo_scavo", "larghezza_scavo"]:
                if self.rows[i].get(key) in [None, ""]:
                    self.rows[i][key] = self.rows[i + 1].get(key)
            if self.rows[i].get("quota_fissa") and self.rows[i].get("invert_elevation") is not None:
                # Constrained node: preserve the fixed invert elevation.
                continue
            L = _to_float(self.rows[i + 1].get("Distance"), 0.0) or 0.0
            J = _to_float(self.rows[i + 1].get("pendenza"), self.spn_pendenza.value()) or self.spn_pendenza.value()
            salto = _to_float(self.rows[i + 1].get("salto_fondo"), 0.0) or 0.0
            self.rows[i]["invert_elevation"] = float(self.rows[i + 1]["invert_elevation"]) + J * L + salto

        for i, r in enumerate(self.rows):
            elev = _to_float(r.get("ground_elevation"), 0.0) or 0.0
            H = _to_float(r.get("invert_elevation"), None)
            D = _to_float(r.get("D"), None)
            r["excavation_depth"] = elev - H if H is not None else None
            r["prof_scav"] = r["excavation_depth"]
            r["q_scorr"] = H
            r["elevaz"] = elev
            r["estradosso"] = (r["excavation_depth"] - D) if (r.get("excavation_depth") is not None and D is not None) else None
            # Cost items.
            if i == 0:
                r["excavation_cost"] = None; r["excavation_code"] = ""; r["excavation_detail"] = ""; r["costo_tot"] = None
            else:
                c, cod, det = _costo_scavo_stratificato(r.get("tipo_scavo"), r.get("excavation_depth"), self.rows[i-1].get("excavation_depth"), r.get("Distance"), r.get("D"))
                r["excavation_cost"] = c; r["excavation_code"] = cod; r["excavation_detail"] = det; r["costo_tot"] = c
            cposa, codposa = _costo_posa(r.get("materiale"), r.get("D"))
            r["pipe_cost"] = cposa; r["pipe_code"] = codposa; r["pipe_cost_tot"] = cposa * (r.get("Distance") or 0.0) if cposa is not None else None
            r["ver_scorrimento"] = (r.get("excavation_depth") or 0) + (D or 0) > 1.0

    def calculate_initial_profile(self):
        try:
            self._push_undo("calcolo iniziale")
            Q = self.spn_q.value() / 1000.0
            J = self.spn_pendenza.value()
            D = float(self.cmb_dn.currentText()) / 1000.0
            mat = self.cmb_materiale.currentText(); K = COEFF_K_MATERIALE.get(mat, 80.0)
            tipo = self.cmb_tipo_scavo.currentText(); larg = self.spn_larghezza.value()
            hi_txt = self.txt_hi_prof.text().strip().replace(",", ".")
            if hi_txt:
                hi = float(hi_txt)
            else:
                hi = (_to_float(self.rows[-1].get("ground_elevation"), 0.0) or 0.0) - 1.0
                self.txt_hi_prof.setText(f"{hi:.3f}")
            for i, r in enumerate(self.rows):
                r["pendenza"] = J if i > 0 else J
                r["D"] = D; r["diam_m"] = D; r["Q"] = Q; r["K"] = K; r["materiale"] = mat.upper(); r["tipo_scavo"] = tipo; r["larghezza_scavo"] = larg
                r["salto_fondo"] = _to_float(r.get("salto_fondo"), 0.0) or 0.0
                if not (r.get("quota_fissa") and r.get("invert_elevation") is not None):
                    r["invert_elevation"] = None
            if not (self.rows[-1].get("quota_fissa") and self.rows[-1].get("invert_elevation") is not None):
                self.rows[-1]["invert_elevation"] = hi
            self._recalculate(); self.run_checks(silent=True); self._refresh_all(); self.status.setText('Profile computed. You can insert invert drops, edit slopes or increase diameters.')
        except Exception as e:
            QMessageBox.critical(self, 'Profile computation error', str(e))


    def _next_manual_node_id(self):
        existing = {str(r.get("node_id")) for r in self.rows}
        base = "M"
        n = 1
        while f"{base}{n}" in existing:
            n += 1
        return f"{base}{n}"

    def _populate_manual_trace_combo(self):
        """Populate the alignment selector used for manual node insertion.

        The alignment was previously inferred from tab 2. If the active
        alignment was not the correct one, or if the editor was reopened to
        modify an existing profile, the user had no way to choose which
        alignment the new node should snap to. The selection is now explicit
        within the profile editor.
        """
        if not hasattr(self, "cmb_manual_trace"):
            return
        current = self.cmb_manual_trace.currentData()
        builder_trace_id = None
        try:
            if hasattr(self.builder, "cmb_tracciato"):
                builder_trace_id = self.builder.cmb_tracciato.currentData()
        except Exception:
            builder_trace_id = None

        self.cmb_manual_trace.blockSignals(True)
        self.cmb_manual_trace.clear()
        self.cmb_manual_trace.addItem('-- select alignment --', "")
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if isinstance(lyr, QgsVectorLayer) and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                    self.cmb_manual_trace.addItem(lyr.name(), lyr.id())
            except Exception:
                pass
        self.cmb_manual_trace.blockSignals(False)

        # Preserve the previous selection; otherwise propose the active alignment
        # from tab 2 when available.
        for wanted in [current, builder_trace_id]:
            if wanted:
                idx = self.cmb_manual_trace.findData(wanted)
                if idx >= 0:
                    self.cmb_manual_trace.setCurrentIndex(idx)
                    return

    def _manual_trace_layer(self):
        """Return the alignment selected in the editor for manual node insertion."""
        layer_id = self.cmb_manual_trace.currentData() if hasattr(self, "cmb_manual_trace") else None
        lyr = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if lyr and isinstance(lyr, QgsVectorLayer) and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
            return lyr
        # Backward-compatible fallback: use the active alignment from tab 2.
        try:
            lyr = self.builder.get_layer(self.builder.cmb_tracciato) if hasattr(self.builder, "cmb_tracciato") else None
            if lyr and QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                return lyr
        except Exception:
            pass
        return None

    def start_add_manual_node(self):
        """Start inserting a new manual node from the QGIS map canvas."""
        try:
            # Refresh the alignment list and ensure that the user explicitly
            # selected the correct reference alignment.
            if hasattr(self, "cmb_manual_trace") and not self.cmb_manual_trace.currentData():
                self._populate_manual_trace_combo()
            if not self._manual_trace_layer():
                QMessageBox.warning(
                    self,
                    'Missing alignment',
                    'Before adding the node, select the line layer in the following field:\n\n'
                    'Reference alignment\n\n'
                    "in the 'Manual node insertion from GIS' section."
                )
                return
            self._cleanup_manual_tool()
            canvas = self.builder.iface.mapCanvas()
            previous_tool = canvas.mapTool()
            snap_layer = self._manual_trace_layer()
            try:
                snap_tol = float(self.builder.spn_snap_tolerance.value()) if hasattr(self.builder, "spn_snap_tolerance") else 2.0
            except Exception:
                snap_tol = 2.0
            self._manual_tool = ManualProfileNodeTool(
                canvas,
                self._finish_add_manual_node,
                self.status.setText,
                previous_tool=previous_tool,
                snap_layer=snap_layer,
                snap_tolerance=snap_tol
            )
            canvas.setMapTool(self._manual_tool)
            try:
                self.setWindowModality(Qt.NonModal)
                self.raise_()
            except Exception:
                pass
            self.status.setText('Tool active: move close to the reference alignment. Blue circle = valid snap, red = outside tolerance. Left-click inserts the node; ESC/right-click cancels.')
        except Exception as e:
            QMessageBox.critical(self, 'Manual node error', str(e))

    def _finish_add_manual_node(self, point_canvas, canvas_crs):
        try:
            self._push_undo('manual node insertion')
            trace_layer = self._manual_trace_layer()
            if not trace_layer:
                raise Exception("Select the 'Reference alignment' in the profile editor where the manual node will be inserted.")
            dtm_layer = QgsProject.instance().mapLayer(self.builder.cmb_dtm.currentData()) if hasattr(self.builder, "cmb_dtm") else None

            geom_point = QgsGeometry.fromPointXY(QgsPointXY(point_canvas))
            pk = None
            node_geom = geom_point
            if trace_layer and QgsWkbTypes.geometryType(trace_layer.wkbType()) == QgsWkbTypes.LineGeometry:
                line_feat = self.builder.first_selected_or_single_line(trace_layer)
                line_geom = line_feat.geometry()
                p_trace = QgsPointXY(point_canvas)
                if canvas_crs != trace_layer.crs():
                    tr = QgsCoordinateTransform(canvas_crs, trace_layer.crs(), QgsProject.instance())
                    p_trace = tr.transform(QgsPointXY(point_canvas))
                p_geom_trace = QgsGeometry.fromPointXY(p_trace)
                pk = float(line_geom.lineLocatePoint(p_geom_trace))
                node_geom = line_geom.interpolate(pk)
            else:
                # Fallback: use the nearest available chainage, appended at the end of the list.
                pk = max([_to_float(r.get("pk"), 0.0) or 0.0 for r in self.rows] + [0.0])

            # Sample the DTM at the inserted/snapped point.
            elev_val = None
            try:
                if dtm_layer and isinstance(dtm_layer, QgsRasterLayer):
                    pt_for_sample = QgsPointXY(node_geom.asPoint())
                    src_crs = trace_layer.crs() if trace_layer else canvas_crs
                    if src_crs != dtm_layer.crs():
                        tr = QgsCoordinateTransform(src_crs, dtm_layer.crs(), QgsProject.instance())
                        pt_for_sample = tr.transform(pt_for_sample)
                    ident = dtm_layer.dataProvider().identify(pt_for_sample, QgsRaster.IdentifyFormatValue)
                    if ident.isValid() and ident.results():
                        elev_val = float(list(ident.results().values())[0])
            except Exception:
                elev_val = None
            if elev_val is None:
                # Interpolate or reuse the ground elevation of nearby nodes as a fallback.
                elev_val = 0.0
                if self.rows:
                    closest = min(self.rows, key=lambda r: abs((_to_float(r.get("pk"), 0.0) or 0.0) - pk))
                    elev_val = _to_float(closest.get("ground_elevation"), 0.0) or 0.0

            node_id = self.txt_manual_node_id.text().strip() or self._next_manual_node_id()
            fixed = self.cmb_manual_node_mode.currentIndex() == 1
            h_fixed = self.spn_manual_fixed_q.value() if fixed else None
            if fixed:
                self.txt_hi_prof.setText(self.txt_hi_prof.text())  # no-op; preserve the user input.

            # Inherit technical parameters from the nearest downstream/upstream node.
            ref = min(self.rows, key=lambda r: abs((_to_float(r.get("pk"), 0.0) or 0.0) - pk)) if self.rows else {}
            new_row = {
                "geometry": QgsGeometry(node_geom),
                "node_id": str(node_id),
                "pk": float(pk),
                "Distance": 0.0,
                "ground_elevation": float(elev_val),
                "elevaz": float(elev_val),
                "invert_elevation": float(h_fixed) if fixed else None,
                "q_scorr": float(h_fixed) if fixed else None,
                "excavation_depth": (float(elev_val) - float(h_fixed)) if fixed else None,
                "prof_scav": (float(elev_val) - float(h_fixed)) if fixed else None,
                "pendenza": ref.get("pendenza", self.spn_pendenza.value()),
                "D": ref.get("D"), "diam_m": ref.get("D"), "Q": ref.get("Q"), "K": ref.get("K"),
                "materiale": ref.get("materiale", ""), "tipo_scavo": ref.get("tipo_scavo", "A"),
                "salto_fondo": 0.0, "larghezza_scavo": ref.get("larghezza_scavo", 1.0),
                "quota_fissa": bool(fixed), "nodo_manuale": True,
            }
            self.rows.append(new_row)
            self.rows.sort(key=lambda r: (float(r.get("pk", 0.0)), str(r.get("node_id", ""))))
            self._recalculate(); self.run_checks(silent=True); self._refresh_all()
            self.status.setText(f"Manual node {node_id} inserted at chainage={pk:.3f}. Invert elevation {'fixed' if fixed else 'variable'}.")
            self.txt_manual_node_id.clear()
        except Exception as e:
            QMessageBox.critical(self, 'Manual node insertion error', str(e))

    def run_checks(self, silent=False):
        try:
            for r in self.rows:
                D = _to_float(r.get("D"), 0.0) or 0.0; K = _to_float(r.get("K"), 0.0) or 0.0; J = _to_float(r.get("pendenza"), 0.0) or 0.0; Q = _to_float(r.get("Q"), 0.0) or 0.0
                q80 = _portata_circolare(D, K, J, 0.8)
                r["Q_max_GR80"] = q80 if q80 > 0 else None
                r["GR"] = Q / q80 if q80 > 0 else None
                r["ok_GR80"] = bool(q80 > 0 and Q / q80 <= 1.0)
                h_rel, q_calc, ok = _trova_tirante_reale(D, K, J, Q)
                if ok and h_rel is not None:
                    A, P, R = _geom_circolare(D, h_rel)
                    v = Q / A if A > 0 else None
                    r["h_rel_reale"] = h_rel; r["depth"] = h_rel * D; r["velocity"] = v; r["OK_velocity"] = bool(v is not None and 0.5 <= v <= 5.0)
                else:
                    r["h_rel_reale"] = None; r["depth"] = None; r["velocity"] = None; r["OK_velocity"] = False
            if not silent:
                self._refresh_all(); self.status.setText('80% filling and velocity checks updated.')
        except Exception as e:
            QMessageBox.critical(self, 'Check error', str(e))

    def insert_drop(self):
        try:
            idx = self._idx_for_node(self.txt_drop_node.text())
            self._push_undo("invert drop")

            # Invert-drop logic:
            # - duplicate the manhole at the same chainage;
            # - the upstream row represents the incoming conduit invert elevation;
            # - the duplicated downstream row represents the manhole invert after the drop.
            #
            # Important edge case for additional pipes:
            # if the drop is inserted at the last node, i.e. the connection node to the
            # pipe, the existing invert must remain the downstream constraint for
            # the pipe. For this reason the duplicated row must NOT be left with
            # altezza=None; otherwise _recalculate() would replace it with ground - 1 m
            # and the connection node depth would be wrong.
            old_h = _to_float(self.rows[idx].get("invert_elevation"), None)
            new_row = dict(self.rows[idx])
            new_row["Distance"] = 0.0
            new_row["salto_fondo"] = self.spn_drop.value()
            if idx == len(self.rows) - 1 and old_h is not None:
                new_row["invert_elevation"] = old_h
                new_row["q_scorr"] = old_h
            else:
                new_row["invert_elevation"] = None
            self.rows.insert(idx + 1, new_row)
            self._recalculate(); self.run_checks(silent=True); self._refresh_all(); self.status.setText(f"Inserito salto dopo node_id {self.rows[idx]['node_id']}.")
        except Exception as e:
            QMessageBox.critical(self, 'Drop error', str(e))

    def modify_slope(self):
        try:
            idx = self._idx_for_node(self.txt_slope_node.text())
            if idx < 1:
                raise ValueError('The selected node is the first one: no conduit segment to edit.')
            self._push_undo('slope change')
            for i in range(1, idx + 1):
                self.rows[i]["pendenza"] = self.spn_new_slope.value()
            self._recalculate(); self.run_checks(silent=True); self._refresh_all(); self.status.setText(f"Slope modified up to node_id {self.rows[idx]['node_id']}.")
        except Exception as e:
            QMessageBox.critical(self, 'Slope error', str(e))

    def increase_diameter(self):
        try:
            idx = self._idx_for_node(self.txt_diam_node.text())
            newD = float(self.cmb_new_dn.currentText()) / 1000.0
            curD = _to_float(self.rows[idx].get("D"), 0.0) or 0.0
            if newD <= curD:
                raise ValueError('The new diameter must be greater than the current one.')
            self._push_undo('aumento diameter')
            for i in range(idx, len(self.rows)):
                self.rows[i]["D"] = newD; self.rows[i]["diam_m"] = newD; self.rows[i]["larghezza_scavo"] = self.spn_new_width.value()
            self._recalculate(); self.run_checks(silent=True); self._refresh_all(); self.status.setText(f"Diameter increased from node_id {self.rows[idx]['node_id']}.")
        except Exception as e:
            QMessageBox.critical(self, 'Diameter error', str(e))

    def _fmt(self, v, nd=3):
        if v is None:
            return ""
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return str(v)

    def _refresh_all(self):
        self._refresh_table(); self._refresh_plot(); self._update_undo_button()

    def _refresh_table(self):
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            for j, c in enumerate(self.table_cols):
                v = r.get(c)
                if c in ["ok_GR80", "OK_velocity", "ver_scorrimento", "quota_fissa", "nodo_manuale"]:
                    txt = "" if v is None else str(bool(v))
                elif c in ["node_id", "materiale", "tipo_scavo"]:
                    txt = str(v if v is not None else "")
                elif c in ["pendenza", "D", "Q", "velocity", "GR", "h_rel_reale"]:
                    txt = self._fmt(v, 4)
                else:
                    txt = self._fmt(v, 3)
                self.table.setItem(i, j, QTableWidgetItem(txt))

    def _refresh_plot(self):
        if not MATPLOTLIB_OK or self.ax is None:
            return
        self.ax.clear()
        if not self.rows:
            self.canvas_plot.draw_idle(); return
        xs = [_to_float(r.get("pk"), 0.0) for r in self.rows]
        ground = [_to_float(r.get("ground_elevation"), None) for r in self.rows]
        H = [_to_float(r.get("invert_elevation"), None) for r in self.rows]
        if any(v is not None for v in ground):
            self.ax.plot(xs, ground, marker="o", linewidth=1.2, label="Terreno")
        for i in range(1, len(self.rows)):
            if H[i-1] is None or H[i] is None:
                continue
            color = "red" if self.rows[i].get("ok_GR80") is False else "blue"
            self.ax.plot([xs[i-1], xs[i]], [H[i-1], H[i]], linewidth=2.2, color=color)
        if any(v is not None for v in H):
            self.ax.scatter(xs, H, s=25, label="Q. scorrimento")
        water = []
        okw = False
        for r in self.rows:
            h = _to_float(r.get("invert_elevation"), None); d = _to_float(r.get("D"), None)
            if h is not None and d is not None:
                water.append(h + 0.8 * d); okw = True
            else:
                water.append(None)
        if okw:
            self.ax.plot(xs, water, linestyle="--", marker="x", linewidth=1.1, label="Acqua 80% D")
        # Highlight manual and fixed-invert nodes with different markers.
        try:
            fixed_x = [float(r.get("pk")) for r in self.rows if r.get("quota_fissa") and r.get("invert_elevation") is not None]
            fixed_y = [float(r.get("invert_elevation")) for r in self.rows if r.get("quota_fissa") and r.get("invert_elevation") is not None]
            if fixed_x:
                self.ax.scatter(fixed_x, fixed_y, marker="s", s=55, label='Fixed-elevation nodes')
            man_x = [float(r.get("pk")) for r in self.rows if r.get("nodo_manuale") and r.get("invert_elevation") is not None]
            man_y = [float(r.get("invert_elevation")) for r in self.rows if r.get("nodo_manuale") and r.get("invert_elevation") is not None]
            if man_x:
                self.ax.scatter(man_x, man_y, marker="D", s=42, label='Manual nodes')
        except Exception:
            pass
        for r in self.rows:
            try:
                suffix = "*" if r.get("quota_fissa") else ""
                self.ax.annotate(str(r.get("node_id")) + suffix, (float(r.get("pk")), float(r.get("ground_elevation"))), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=8)
            except Exception:
                pass
        self.ax.set_title('Longitudinal profile')
        self.ax.set_xlabel("pk [m]"); self.ax.set_ylabel('Elevation [m]'); self.ax.grid(True); self.ax.legend(loc="best")
        self.fig.tight_layout(); self.canvas_plot.draw_idle()

    def _make_memory_layer(self, layer_name="manhole_calculated"):
        fields = QgsFields()
        field_defs = [
            ("node_id", QVariant.String), ("elevaz", QVariant.Double), ("ground_elevation", QVariant.Double), ("q_scorr", QVariant.Double), ("invert_elevation", QVariant.Double),
            ("prof_scav", QVariant.Double), ("excavation_depth", QVariant.Double), ("Distance", QVariant.Double), ("pk", QVariant.Double),
            ("pendenza", QVariant.Double), ("diam_m", QVariant.Double), ("D", QVariant.Double), ("materiale", QVariant.String), ("tipo_scavo", QVariant.String),
            ("Q", QVariant.Double), ("K", QVariant.Double), ("salto_fondo", QVariant.Double), ("salto_f", QVariant.Double), ("larg_scav", QVariant.Double),
            ("quota_fissa", QVariant.Int), ("nodo_man", QVariant.Int),
            ("estradosso", QVariant.Double), ("Q_max_GR80", QVariant.Double), ("sat80", QVariant.Double), ("tir_reale", QVariant.Double), ("h_rel", QVariant.Double),
            ("velocity", QVariant.Double), ("ok80", QVariant.Int), ("ok_vel", QVariant.Int), ("ver_scorr", QVariant.Int),
            ("c_scavo", QVariant.Double), ("cod_scavo", QVariant.String), ("det_scavo", QVariant.String), ("c_posa", QVariant.Double), ("cod_posa", QVariant.String), ("c_posa_t", QVariant.Double)
        ]
        for n, t in field_defs:
            fields.append(QgsField(n, t))
        mem = QgsVectorLayer(f"Point?crs={self.point_layer.crs().authid()}", layer_name, "memory")
        pr = mem.dataProvider(); pr.addAttributes(fields); mem.updateFields()
        for r in self.rows:
            f = QgsFeature(mem.fields()); f.setGeometry(QgsGeometry(r["geometry"]))
            vals = {
                "node_id": r.get("node_id"), "elevaz": r.get("ground_elevation"), "ground_elevation": r.get("ground_elevation"), "q_scorr": r.get("invert_elevation"), "invert_elevation": r.get("invert_elevation"),
                "prof_scav": r.get("excavation_depth"), "excavation_depth": r.get("excavation_depth"), "Distance": r.get("Distance"), "pk": r.get("pk"),
                "pendenza": r.get("pendenza"), "diam_m": r.get("D"), "D": r.get("D"), "materiale": r.get("materiale"), "tipo_scavo": r.get("tipo_scavo"),
                "Q": r.get("Q"), "K": r.get("K"), "salto_fondo": r.get("salto_fondo"), "salto_f": r.get("salto_fondo"), "larg_scav": r.get("larghezza_scavo"),
                "quota_fissa": 1 if r.get("quota_fissa") else 0, "nodo_man": 1 if r.get("nodo_manuale") else 0,
                "estradosso": r.get("estradosso"), "Q_max_GR80": r.get("Q_max_GR80"), "sat80": r.get("GR"), "tir_reale": r.get("depth"), "h_rel": r.get("h_rel_reale"),
                "velocity": r.get("velocity"), "ok80": 1 if r.get("ok_GR80") else 0, "ok_vel": 1 if r.get("OK_velocity") else 0, "ver_scorr": 1 if r.get("ver_scorrimento") else 0,
                "c_scavo": r.get("excavation_cost"), "cod_scavo": r.get("excavation_code"), "det_scavo": str(r.get("excavation_detail") or "")[:254],
                "c_posa": r.get("pipe_cost"), "cod_posa": r.get("pipe_code"), "c_posa_t": r.get("pipe_cost_tot")
            }
            for k, v in vals.items():
                if k in mem.fields().names():
                    f[k] = v
            pr.addFeature(f)
        mem.updateExtents()
        return mem

    def _write_csv(self, path):
        cols = self.table_cols + ["K", "materiale", "tipo_scavo", "larghezza_scavo", "estradosso", "quota_fissa", "nodo_manuale", "excavation_code", "excavation_detail", "pipe_code"]
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(";".join(cols) + "\n")
            for r in self.rows:
                vals = []
                for c in cols:
                    v = r.get(c)
                    vals.append("" if v is None else str(v).replace(";", ","))
                f.write(";".join(vals) + "\n")

    def _write_simple_dxf(self, path):
        def line_ent(x1, y1, x2, y2, layer="0"):
            return f"0\nLINE\n8\n{layer}\n10\n{x1:.3f}\n20\n{y1:.3f}\n30\n0\n11\n{x2:.3f}\n21\n{y2:.3f}\n31\n0\n"
        def text_ent(x, y, txt, h=1.5, layer="TEXT"):
            txt = str(txt).replace("\n", " ")
            return f"0\nTEXT\n8\n{layer}\n10\n{x:.3f}\n20\n{y:.3f}\n30\n0\n40\n{h:.3f}\n1\n{txt}\n"
        sx, sy = 1.0, 10.0
        xs = [_to_float(r.get("pk"), 0.0) * sx for r in self.rows]
        gr = [_to_float(r.get("ground_elevation"), 0.0) * sy for r in self.rows]
        sc = [_to_float(r.get("invert_elevation"), 0.0) * sy for r in self.rows]
        data = "0\nSECTION\n2\nENTITIES\n"
        for i in range(1, len(self.rows)):
            data += line_ent(xs[i-1], gr[i-1], xs[i], gr[i], "TERRENO")
            data += line_ent(xs[i-1], sc[i-1], xs[i], sc[i], "SCORRIMENTO")
        for i, r in enumerate(self.rows):
            data += line_ent(xs[i], sc[i], xs[i], gr[i], "POZZETTI")
            data += text_ent(xs[i], gr[i] + 5, f"N{r.get('node_id')}", 2.0)
            data += text_ent(xs[i], sc[i] - 5, f"Qsc={_to_float(r.get('invert_elevation'),0):.2f}", 1.5)
        data += text_ent(xs[0] if xs else 0, max(gr + sc) + 20 if xs else 0, 'SEWER NETWORK LONGITUDINAL PROFILE', 3.0)
        data += "0\nENDSEC\n0\nEOF\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def save_outputs(self):
        try:
            self._recalculate(); self.run_checks(silent=True)
            os.makedirs(self.outdir, exist_ok=True)
            base_name = "manhole_calculated"
            try:
                lyr_name = self.point_layer.name().replace(" ", "_")
                lyr_name_clean = lyr_name.strip()
                lyr_low = lyr_name_clean.lower()

                # For additional pipes, use the prefix defined by the user in the
                # "New sewer node prefix" field. The prefix is stored in the
                # branch/branch_prefix field of the pozzetti_da_tracciato_<prefix> layer.
                # This makes the profile output name explicit, for example:
                # manhole_calculated_A.gpkg, manhole_calculated_B.gpkg, and so on.
                branch_prefix = ""
                try:
                    names = self.point_layer.fields().names()
                    for fld in ["branch", "branch_prefix", "prefisso"]:
                        if fld in names:
                            for feat in self.point_layer.getFeatures():
                                val = feat[fld]
                                if val not in [None, ""]:
                                    branch_prefix = re.sub(r"[^A-Za-z0-9]", "", str(val).strip().upper())[:10]
                                    if branch_prefix:
                                        break
                            if branch_prefix:
                                break
                except Exception:
                    branch_prefix = ""

                # Robust fallback: when working on an additional pipe, the user-selected
                # prefix remains available in the main Sewer Builder dialog even if the
                # point layer does not yet contain branch/branch_prefix fields or has a
                # generic name.
                if not branch_prefix:
                    try:
                        txt = getattr(getattr(self, "builder", None), "txt_branch_prefix", None)
                        if txt is not None:
                            v = re.sub(r"[^A-Za-z0-9]", "", txt.text().strip().upper())
                            # Use it only for layers that actually belong to additional pipes,
                            # to avoid accidentally renaming the main pipe.
                            if v and ("tracciato" in lyr_low or "pozzetti_da" in lyr_low or "ramo" in lyr_low):
                                branch_prefix = v[:10]
                    except Exception:
                        pass

                if not branch_prefix:
                    for pat in [
                        r"pozzetti_da_tracciato_([A-Za-z0-9]+)$",
                        r"tracciato_aggiunto_([A-Za-z0-9]+)$",
                        r"manhole_calculated_([A-Za-z0-9]+)$",
                    ]:
                        m = re.search(pat, lyr_name_clean, re.IGNORECASE)
                        if m:
                            branch_prefix = re.sub(r"[^A-Za-z0-9]", "", m.group(1).strip().upper())[:10]
                            if branch_prefix:
                                break

                if branch_prefix:
                    base_name = f"manhole_calculated_{branch_prefix}"
                # When reopening an already computed profile, save it with the same base name.
                # This allows profile corrections after conduits have already been created
                # without generating names such as manhole_calculated_manhole_calculated_A.
                elif lyr_low.startswith("manhole_calculated"):
                    base_name = lyr_name_clean
                elif lyr_name_clean and lyr_low not in ("pozzetti_da_tracciato", "pozzetti"):
                    base_name = "manhole_calculated_" + lyr_name_clean
            except Exception:
                pass
            out_path = os.path.join(self.outdir, base_name + ".gpkg")
            mem = self._make_memory_layer(base_name)
            driver = "GPKG"
            QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", mem.crs(), driver)
            lyr = QgsVectorLayer(out_path, base_name, "ogr")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                self.output_layer = lyr
                self.builder.profile_nodes_layer = lyr
                self.builder.refresh_layers()
                idx = self.builder.cmb_nodi.findData(lyr.id())
                if idx >= 0:
                    self.builder.cmb_nodi.setCurrentIndex(idx)
            base = os.path.splitext(out_path)[0]
            self._write_csv(base + "_calcoli.csv")
            self._write_simple_dxf(base + "_profilo.dxf")
            QMessageBox.information(self, "Output salvati", f"Creati:\n{out_path}\n{base}_calcoli.csv\n{base}_profilo.dxf")
            self.status.setText('Profile outputs saved and layer loaded into the QGIS project.')
        except Exception as e:
            QMessageBox.critical(self, 'Save error', str(e))

    def _cleanup_manual_tool(self):
        """Disable any manual-node map tool owned by this profile editor."""
        try:
            canvas = self.builder.iface.mapCanvas() if getattr(self, "builder", None) is not None else None
        except Exception:
            canvas = None

        tools = []
        tool = getattr(self, "_manual_tool", None)
        if tool is not None:
            tools.append(tool)
        try:
            current_tool = canvas.mapTool() if canvas is not None else None
            if isinstance(current_tool, ManualProfileNodeTool) and current_tool not in tools:
                tools.append(current_tool)
        except Exception:
            current_tool = None

        for tool in tools:
            try:
                tool.message_callback = None
            except Exception:
                pass
            try:
                tool.deactivate_tool(restore_tool=True)
            except Exception:
                try:
                    tool.clear_drawing(restore_tool=True)
                except Exception:
                    pass

        try:
            if canvas is not None and isinstance(canvas.mapTool(), ManualProfileNodeTool):
                canvas.unsetMapTool(canvas.mapTool())
        except Exception:
            pass
        try:
            if canvas is not None and isinstance(canvas.mapTool(), ManualProfileNodeTool):
                self.builder.iface.actionPan().trigger()
        except Exception:
            pass
        self._manual_tool = None

    def accept(self):
        self._cleanup_manual_tool()
        super().accept()

    def reject(self):
        self._cleanup_manual_tool()
        super().reject()

    def closeEvent(self, event):
        self._cleanup_manual_tool()
        super().closeEvent(event)



class MergeSwmmInputsDialog(QDialog):
    """Dialog used to merge multiple manhole and conduit layers into one SWMM input set."""
    def __init__(self, parent, point_layers, line_layers, outdir):
        super().__init__(parent)
        self.setWindowTitle('Prepare SWMM input - merge conduits and manholes')
        self.resize(620, 520)
        self.point_layers = point_layers
        self.line_layers = line_layers
        layout = QVBoxLayout(self)

        info = QLabel(
            'Select all computed conduit and manhole layers to be used in the SWMM model. '
            "The plugin will create two consolidated GeoPackages: input_swmm_tratte and input_swmm_pozzetti."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        row = QHBoxLayout()
        box_lines = QGroupBox('Conduits / links to merge')
        l1 = QVBoxLayout(box_lines)
        self.lst_lines = QListWidget()
        for lyr in line_layers:
            it = QListWidgetItem(lyr.name())
            it.setData(Qt.UserRole, lyr.id())
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            checked = "tratte" in lyr.name().lower() or "condotte" in lyr.name().lower()
            it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.lst_lines.addItem(it)
        l1.addWidget(self.lst_lines)
        row.addWidget(box_lines)

        box_pts = QGroupBox('Manholes / nodes to merge')
        l2 = QVBoxLayout(box_pts)
        self.lst_points = QListWidget()
        for lyr in point_layers:
            it = QListWidgetItem(lyr.name())
            it.setData(Qt.UserRole, lyr.id())
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            checked = "pozz" in lyr.name().lower() or "nodi" in lyr.name().lower()
            it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.lst_points.addItem(it)
        l2.addWidget(self.lst_points)
        row.addWidget(box_pts)
        layout.addLayout(row)

        form = QFormLayout()
        self.txt_outdir = QLineEdit(outdir or os.path.expanduser("~"))
        btn = QPushButton("Sfoglia")
        r = QHBoxLayout(); r.addWidget(self.txt_outdir); r.addWidget(btn)
        btn.clicked.connect(self._choose_outdir)
        form.addRow('Output folder:', r)
        self.txt_lines_name = QLineEdit("input_swmm_tratte.gpkg")
        self.txt_points_name = QLineEdit("input_swmm_pozzetti.gpkg")
        form.addRow('Merged conduit file:', self.txt_lines_name)
        form.addRow('Merged manhole file:', self.txt_points_name)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_outdir(self):
        d = QFileDialog.getExistingDirectory(self, 'Select output folder', self.txt_outdir.text().strip() or os.path.expanduser("~"))
        if d:
            self.txt_outdir.setText(d)

    def _checked_ids(self, widget):
        ids = []
        for i in range(widget.count()):
            it = widget.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        return ids

    def selected_line_ids(self):
        return self._checked_ids(self.lst_lines)

    def selected_point_ids(self):
        return self._checked_ids(self.lst_points)

    def output_dir(self):
        return self.txt_outdir.text().strip() or os.path.expanduser("~")

    def output_lines_name(self):
        return self.txt_lines_name.text().strip() or "input_swmm_tratte.gpkg"

    def output_points_name(self):
        return self.txt_points_name.text().strip() or "input_swmm_pozzetti.gpkg"


class SewerSWMMBuilderDialog(QDialog):
    def __init__(self, iface, plugin_dir, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.canvas = iface.mapCanvas()
        self.basin_geom = None
        self.basin_crs = None
        self.draw_tool = None
        self.trace_draw_tool = None
        self.pump_draw_tool = None
        self.regulator_draw_tool = None
        self.extra_node_draw_tool = None
        self.manual_link_draw_tool = None
        self.orifice_definitions = []
        self.weir_definitions = []
        self.pump_definitions = []
        self.manual_link_definitions = []
        self.subcatch_layer = None
        self.trace_nodes_layer = None
        self.profile_nodes_layer = None
        self.tratte_layer = None
        self.collector_workflow_locked = False
        self.setWindowTitle("SWMM Sewer Builder")
        self.resize(760, 650)
        self._build_ui()
        self.refresh_layers()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        def make_scroll_tab(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(10, 10, 10, 10)
            page_layout.setSpacing(8)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, title)
            return page_layout

        tab_input = make_scroll_tab("1 - Impostazioni")
        tab_sewer = make_scroll_tab("2 - Sewer Builder")
        tab_swmm = make_scroll_tab('3 - Catchment / SWMM')
        tab_output = make_scroll_tab("4 - Input SWMM / Output / Log")

        intro = QLabel('Set the main working folder. All files generated by the plugin are saved automatically in this folder, without intermediate save dialogs.')
        intro.setWordWrap(True)
        tab_input.addWidget(intro)

        box_workdir = QGroupBox('Working / output folder')
        l_workdir = QVBoxLayout(box_workdir)
        row_main_out = QHBoxLayout()
        row_main_out.addWidget(QLabel('Output folder:'))
        self.txt_outdir = QLineEdit(os.path.expanduser("~"))
        row_main_out.addWidget(self.txt_outdir)
        btn_main_out = QPushButton("Sfoglia")
        btn_main_out.clicked.connect(self.choose_outdir)
        row_main_out.addWidget(btn_main_out)
        l_workdir.addLayout(row_main_out)
        note_out = QLabel('Used for manholes, profiles, conduits, added alignments, SWMM input, subcatchments, INP/RPT/OUT files and results.')
        note_out.setWordWrap(True)
        l_workdir.addWidget(note_out)
        tab_input.addWidget(box_workdir)
        tab_input.addStretch(1)

        box_layers = QGroupBox('SWMM input for .INP generation')
        l_layers = QVBoxLayout(box_layers)
        note_swmm_layers = QLabel(
            "These layers are used only to create the SWMM .INP file. "
            "To connect new pipes, use the 'Existing conduits to connect to' field in tab 2."
        )
        note_swmm_layers.setWordWrap(True)
        l_layers.addWidget(note_swmm_layers)

        row_cond = QHBoxLayout()
        row_cond.addWidget(QLabel('Conduit/link input layer for SWMM:'))
        self.cmb_condotte = QComboBox()
        row_cond.addWidget(self.cmb_condotte)
        l_layers.addLayout(row_cond)

        row_nodi = QHBoxLayout()
        row_nodi.addWidget(QLabel('Node/manhole input layer for SWMM:'))
        self.cmb_nodi = QComboBox()
        row_nodi.addWidget(self.cmb_nodi)
        l_layers.addLayout(row_nodi)

        btn_refresh = QPushButton('Refresh layer list')
        btn_refresh.clicked.connect(self.refresh_layers)
        l_layers.addWidget(btn_refresh)

        # Tab 4 is dedicated only to SWMM inputs and INP/RPT/OUT model generation.
        # The old generic manhole creation function is no longer exposed here to avoid confusion
        # with the guided workflow in tab 2.

        box_sewer = QGroupBox('Sewer Builder - network design from alignment + DEM')
        l_sewer = QVBoxLayout(box_sewer)

        intro_sewer = QLabel(
            'This tab is organized in two phases: first create the <b>main pipe</b>, '
            "then repeat the same steps for each <b>additional pipe</b>. "
            "Steps <b>1 → 2 → 3</b> are always required to produce layers usable in SWMM."
        )
        intro_sewer.setWordWrap(True)
        l_sewer.addWidget(intro_sewer)

        box_active_trace = QGroupBox('Active alignment to process')
        l_active_trace = QVBoxLayout(box_active_trace)
        note_active_trace = QLabel(
            'Select the alignment on which steps 1 → 2 → 3 will be run. '
            'When drawing an additional pipe, the plugin automatically sets this field to the newly added alignment.'
        )
        note_active_trace.setWordWrap(True)
        l_active_trace.addWidget(note_active_trace)

        row_trace = QHBoxLayout()
        row_trace.addWidget(QLabel('Active design alignment:'))
        self.cmb_tracciato = QComboBox()
        row_trace.addWidget(self.cmb_tracciato)
        l_active_trace.addLayout(row_trace)

        row_dtm = QHBoxLayout()
        row_dtm.addWidget(QLabel("Ground DTM/raster:"))
        self.cmb_dtm = QComboBox()
        row_dtm.addWidget(self.cmb_dtm)
        row_dtm.addWidget(QLabel('Manhole spacing [m]:'))
        self.spn_node_interval = QDoubleSpinBox()
        self.spn_node_interval.setRange(1.0, 1000.0)
        self.spn_node_interval.setDecimals(1)
        self.spn_node_interval.setValue(50.0)
        row_dtm.addWidget(self.spn_node_interval)
        l_active_trace.addLayout(row_dtm)
        l_sewer.addWidget(box_active_trace)

        # Profile design parameters are no longer exposed in the Sewer Builder tab:
        # downstream invert Hi, flow rate, slope, diameter, material and excavation type
        # are set directly in the manhole profile editor, where they can also be edited with undo.
        self.txt_hi = QLineEdit("")
        self.txt_hi.setVisible(False)

        note_profile_params = QLabel(
            'Downstream elevation Hi, flow Q, design slope, DN, material and excavation type '
            'are set and edited only in the manhole profile editor.'
        )
        note_profile_params.setWordWrap(True)
        l_sewer.addWidget(note_profile_params)

        self.box_main_workflow = QGroupBox("A) Step 1 - Main pipe (to be completed only once)")
        l_main_workflow = QVBoxLayout(self.box_main_workflow)
        self.lbl_collector_state = QLabel(
            "<b>Status:</b> main pipe not created yet.<br>"
            "Select the main alignment in the 'Active alignment' field and follow steps 1 → 2 → 3 below."
        )
        self.lbl_collector_state.setWordWrap(True)
        l_main_workflow.addWidget(self.lbl_collector_state)

        row_sewer_btn = QHBoxLayout()
        self.btn_nodes_from_trace = QPushButton('1) Create pipe manholes')
        self.btn_nodes_from_trace.setToolTip('Required step 1: generate manholes along the main alignment and sample the DEM.')
        self.btn_nodes_from_trace.clicked.connect(self.create_nodes_from_trace_dtm)
        row_sewer_btn.addWidget(self.btn_nodes_from_trace)
        self.btn_calc_profile = QPushButton('2) Open pipe profile editor')
        self.btn_calc_profile.setToolTip('Required step 2: compute/edit the main profile, then save manhole_calculated.')
        self.btn_calc_profile.clicked.connect(self.calculate_profile_from_nodes)
        row_sewer_btn.addWidget(self.btn_calc_profile)
        self.btn_segments_from_profile = QPushButton('3) Create pipe conduits')
        self.btn_segments_from_profile.setToolTip('Required step 3: create the conduits/links of the main pipe.')
        self.btn_segments_from_profile.clicked.connect(self.create_segments_from_profile_nodes)
        row_sewer_btn.addWidget(self.btn_segments_from_profile)
        l_main_workflow.addLayout(row_sewer_btn)

        self.btn_unlock_collector = QPushButton("Unlock main pipe for corrections")
        self.btn_unlock_collector.setToolTip("Use this only if you need to repeat the main pipe steps.")
        self.btn_unlock_collector.clicked.connect(lambda: self.set_collector_workflow_locked(False, manual=True))
        self.btn_unlock_collector.setVisible(False)
        l_main_workflow.addWidget(self.btn_unlock_collector)

        box_main_edit = QGroupBox("Corrections after creating the main pipe")
        l_main_edit = QVBoxLayout(box_main_edit)
        note_main_edit = QLabel(
            'If you have already completed steps 1 → 2 → 3 but need to correct the profile, '
            'select the pipe manhole_calculated layer in the node/manhole input field, '
            'open the editor, save, and then regenerate the conduits. There is no need to recreate the manholes from the DEM.'
        )
        note_main_edit.setWordWrap(True)
        l_main_edit.addWidget(note_main_edit)

        row_main_edit_layer = QHBoxLayout()
        row_main_edit_layer.addWidget(QLabel("Computed manhole layer to edit:"))
        self.cmb_main_edit_pozzetti = QComboBox()
        self.cmb_main_edit_pozzetti.setToolTip('Select the pipe point layer manhole_calculated to reopen in the profile editor.')
        row_main_edit_layer.addWidget(self.cmb_main_edit_pozzetti)
        l_main_edit.addLayout(row_main_edit_layer)

        row_main_edit = QHBoxLayout()
        self.btn_main_edit_existing_profile = QPushButton('Edit existing pipe profile')
        self.btn_main_edit_existing_profile.clicked.connect(lambda: self.edit_existing_profile_from_combo(self.cmb_main_edit_pozzetti))
        row_main_edit.addWidget(self.btn_main_edit_existing_profile)
        self.btn_main_regen_segments = QPushButton('Regenerate pipe conduits after editing')
        self.btn_main_regen_segments.clicked.connect(lambda: self.regen_segments_from_profile_combo(self.cmb_main_edit_pozzetti))
        row_main_edit.addWidget(self.btn_main_regen_segments)
        l_main_edit.addLayout(row_main_edit)
        l_main_workflow.addWidget(box_main_edit)
        l_sewer.addWidget(self.box_main_workflow)

        box_add = QGroupBox("B) Step 2 - Additional pipes")
        l_add = QVBoxLayout(box_add)
        note_branch_workflow = QLabel(
            'After creating the main pipe, select the existing conduits to connect to, '
            'draw the new pipe and then follow steps 1 → 2 → 3 below.'
        )
        note_branch_workflow.setWordWrap(True)
        l_add.addWidget(note_branch_workflow)

        row_snap_layer = QHBoxLayout()
        row_snap_layer.addWidget(QLabel('Existing conduits to connect to:'))
        self.cmb_snap_tratte = QComboBox()
        self.cmb_snap_tratte.setToolTip('Select the pipe_calculated layer of the pipe to connect the new pipe to.')
        row_snap_layer.addWidget(self.cmb_snap_tratte)
        l_add.addLayout(row_snap_layer)

        row_snap_nodes = QHBoxLayout()
        row_snap_nodes.addWidget(QLabel('Existing nodes to connect to (optional):'))
        self.cmb_snap_nodi = QComboBox()
        self.cmb_snap_nodi.setToolTip('Select the already computed manhole/node layer. While drawing, the pipe can snap directly to an existing node without splitting the conduit.')
        row_snap_nodes.addWidget(self.cmb_snap_nodi)
        l_add.addLayout(row_snap_nodes)

        row_add1 = QHBoxLayout()
        row_add1.addWidget(QLabel('New sewer node prefix:'))
        self.txt_branch_prefix = QLineEdit("")
        self.txt_branch_prefix.setPlaceholderText("auto: A, B, C...")
        row_add1.addWidget(self.txt_branch_prefix)
        row_add1.addWidget(QLabel("Snap tolerance [m]:"))
        self.spn_snap_tolerance = QDoubleSpinBox()
        self.spn_snap_tolerance.setRange(0.01, 100.0)
        self.spn_snap_tolerance.setDecimals(2)
        self.spn_snap_tolerance.setValue(2.00)
        row_add1.addWidget(self.spn_snap_tolerance)
        l_add.addLayout(row_add1)
        self.btn_draw_new_trace = QPushButton('Draw new connected pipe')
        self.btn_draw_new_trace.setToolTip('Draw a new polyline; while drawing, the snap point is displayed. Green = valid snap, red = outside tolerance.')
        self.btn_draw_new_trace.clicked.connect(self.start_draw_connected_trace)
        l_add.addWidget(self.btn_draw_new_trace)

        note_branch_steps = QLabel(
            "<b>Required steps for the newly drawn pipe:</b> "
            'after right-clicking, the new alignment automatically becomes the active alignment; '
            "click steps 1 → 2 → 3 in sequence."
        )
        note_branch_steps.setWordWrap(True)
        l_add.addWidget(note_branch_steps)

        row_branch_btn = QHBoxLayout()
        self.btn_branch_nodes_from_trace = QPushButton('1) Create pipe manholes')
        self.btn_branch_nodes_from_trace.setToolTip('Step 1 for the pipe: generate manholes on the active alignment and sample the DEM.')
        self.btn_branch_nodes_from_trace.clicked.connect(self.create_nodes_from_trace_dtm)
        row_branch_btn.addWidget(self.btn_branch_nodes_from_trace)
        self.btn_branch_calc_profile = QPushButton('2) Open pipe profile editor')
        self.btn_branch_calc_profile.setToolTip('Step 2 for the pipe: compute/edit the profile and save the pipe manhole_calculated layer.')
        self.btn_branch_calc_profile.clicked.connect(self.calculate_profile_from_nodes)
        row_branch_btn.addWidget(self.btn_branch_calc_profile)
        self.btn_branch_segments_from_profile = QPushButton('3) Create pipe conduits')
        self.btn_branch_segments_from_profile.setToolTip('Step 3 for the pipe: create pipe conduits from the saved profile.')
        self.btn_branch_segments_from_profile.clicked.connect(self.create_segments_from_profile_nodes)
        row_branch_btn.addWidget(self.btn_branch_segments_from_profile)
        l_add.addLayout(row_branch_btn)

        box_branch_edit = QGroupBox('Corrections after creating pipe conduits')
        l_branch_edit = QVBoxLayout(box_branch_edit)
        note_branch_edit = QLabel(
            'If you need to make changes after creating the pipe manholes, profile and conduits, '
            'do not recreate the pipe: select the related manhole_calculated layer, reopen the editor, save, '
            'then regenerate the pipe conduits. The plugin reuses the active alignment and overwrites the pipe pipe_calculated file.'
        )
        note_branch_edit.setWordWrap(True)
        l_branch_edit.addWidget(note_branch_edit)

        row_branch_edit_layer = QHBoxLayout()
        row_branch_edit_layer.addWidget(QLabel("Pipe computed manhole layer to edit:"))
        self.cmb_branch_edit_pozzetti = QComboBox()
        self.cmb_branch_edit_pozzetti.setToolTip('Select the pipe point layer manhole_calculated to reopen in the profile editor.')
        row_branch_edit_layer.addWidget(self.cmb_branch_edit_pozzetti)
        l_branch_edit.addLayout(row_branch_edit_layer)

        row_branch_edit = QHBoxLayout()
        self.btn_branch_edit_existing_profile = QPushButton('Edit existing pipe profile')
        self.btn_branch_edit_existing_profile.clicked.connect(lambda: self.edit_existing_profile_from_combo(self.cmb_branch_edit_pozzetti))
        row_branch_edit.addWidget(self.btn_branch_edit_existing_profile)
        self.btn_branch_regen_segments = QPushButton('Regenerate pipe conduits after editing')
        self.btn_branch_regen_segments.clicked.connect(lambda: self.regen_segments_from_profile_combo(self.cmb_branch_edit_pozzetti))
        row_branch_edit.addWidget(self.btn_branch_regen_segments)
        l_branch_edit.addLayout(row_branch_edit)
        l_add.addWidget(box_branch_edit)
        l_sewer.addWidget(box_add)

        tab_sewer.addWidget(box_sewer)
        tab_sewer.addStretch(1)

        box_basin = QGroupBox('Drainage catchment')
        l_basin = QVBoxLayout(box_basin)
        self.lbl_basin = QLabel('No polygon acquired.')
        l_basin.addWidget(self.lbl_basin)
        row_basin = QHBoxLayout()
        self.btn_draw = QPushButton('Draw catchment polygon on map')
        self.btn_draw.clicked.connect(self.start_draw_basin)
        row_basin.addWidget(self.btn_draw)
        self.btn_clear = QPushButton("Clear polygon")
        self.btn_clear.clicked.connect(self.clear_basin)
        row_basin.addWidget(self.btn_clear)
        l_basin.addLayout(row_basin)
        tab_swmm.addWidget(box_basin)

        box_params = QGroupBox("Hydrologic and rainfall parameters")
        l_params = QVBoxLayout(box_params)

        rain_split = QHBoxLayout()

        box_hydro = QGroupBox('Subcatchment hydrologic parameters')
        l_hydro = QVBoxLayout(box_hydro)
        row_imp_mode = QHBoxLayout()
        row_imp_mode.addWidget(QLabel("Metodo % impervious:"))
        self.cmb_imperv_mode = QComboBox()
        self.cmb_imperv_mode.addItems(['Manual', 'Compute from roofs/roads'])
        self.cmb_imperv_mode.setToolTip(
            'Manual = use the percentage specified below. '
            'Compute from roofs/roads = intersect each subcatchment with the selected polygon layers '
            "and compute a weighted average using the runoff coefficients."
        )
        row_imp_mode.addWidget(self.cmb_imperv_mode)
        l_hydro.addLayout(row_imp_mode)

        row_imp = QHBoxLayout()
        row_imp.addWidget(QLabel('Manual imperviousness [%]:'))
        self.spn_imperv = QDoubleSpinBox()
        self.spn_imperv.setRange(0, 100)
        self.spn_imperv.setDecimals(1)
        self.spn_imperv.setValue(70.0)
        self.spn_imperv.setToolTip('Value used when the method is Manual or as fallback if the surface-based calculation does not produce valid data.')
        row_imp.addWidget(self.spn_imperv)
        l_hydro.addLayout(row_imp)

        box_imperv_layers = QGroupBox("Imperviousness calculation from surfaces")
        l_imp_layers = QGridLayout(box_imperv_layers)

        l_imp_layers.addWidget(QLabel('Roof layer:'), 0, 0)
        self.cmb_imperv_roofs = QComboBox()
        self.cmb_imperv_roofs.addItem('No roof layer', "")
        self.cmb_imperv_roofs.setToolTip('Polygon layer of roofs/building covers. It will be intersected with the subcatchments.')
        l_imp_layers.addWidget(self.cmb_imperv_roofs, 0, 1)
        l_imp_layers.addWidget(QLabel("Coeff. afflusso tetti:"), 0, 2)
        self.spn_coeff_roofs = QDoubleSpinBox()
        self.spn_coeff_roofs.setRange(0.0, 1.0)
        self.spn_coeff_roofs.setDecimals(3)
        self.spn_coeff_roofs.setSingleStep(0.05)
        self.spn_coeff_roofs.setValue(0.90)
        l_imp_layers.addWidget(self.spn_coeff_roofs, 0, 3)

        l_imp_layers.addWidget(QLabel('Road layer:'), 1, 0)
        self.cmb_imperv_roads = QComboBox()
        self.cmb_imperv_roads.addItem('No road layer', "")
        self.cmb_imperv_roads.setToolTip('Polygon layer of road/paved areas. It will be intersected with the subcatchments.')
        l_imp_layers.addWidget(self.cmb_imperv_roads, 1, 1)
        l_imp_layers.addWidget(QLabel("Coeff. afflusso strade:"), 1, 2)
        self.spn_coeff_roads = QDoubleSpinBox()
        self.spn_coeff_roads.setRange(0.0, 1.0)
        self.spn_coeff_roads.setDecimals(3)
        self.spn_coeff_roads.setSingleStep(0.05)
        self.spn_coeff_roads.setValue(0.85)
        l_imp_layers.addWidget(self.spn_coeff_roads, 1, 3)

        lbl_imp_note = QLabel(
            "%Impervious = ((Area tetti interna × coeff. tetti) + "
            "(internal road area × road coefficient)) / subcatchment area × 100. "
            'When this method is enabled, the manual value does not constrain the result: if there are no intersections, the computed value is 0%.'
        )
        lbl_imp_note.setWordWrap(True)
        l_imp_layers.addWidget(lbl_imp_note, 2, 0, 1, 4)
        l_hydro.addWidget(box_imperv_layers)

        row_sub_slope = QHBoxLayout()
        row_sub_slope.addWidget(QLabel('Subcatchment slope [m/m]:'))
        self.spn_slope = QDoubleSpinBox()
        self.spn_slope.setRange(0.0001, 1.0)
        self.spn_slope.setDecimals(4)
        self.spn_slope.setSingleStep(0.001)
        self.spn_slope.setValue(0.005)
        row_sub_slope.addWidget(self.spn_slope)
        l_hydro.addLayout(row_sub_slope)

        self.chk_slope_dtm = QCheckBox('Compute DEM slope for each subcatchment')
        self.chk_slope_dtm.setToolTip(
            'If enabled, each subcatchment slope is estimated by sampling the DEM '
            "at polygon vertices and at the centroid. The value above remains available as a fallback."
        )
        l_hydro.addWidget(self.chk_slope_dtm)

        lbl_slope_dtm_note = QLabel(
            "DEM slope: lightweight sampling at vertices and centroid; output is always in m/m, "
            "poi convertito in % nel file INP."
        )
        lbl_slope_dtm_note.setWordWrap(True)
        l_hydro.addWidget(lbl_slope_dtm_note)
        rain_split.addWidget(box_hydro, 1)

        box_rain = QGroupBox('Design storm / hyetograph definition')
        l_rain_box = QVBoxLayout(box_rain)

        row_rain_type = QHBoxLayout()
        row_rain_type.addWidget(QLabel('Storm type:'))
        self.cmb_rain_type = QComboBox()
        self.cmb_rain_type.addItems(["Rettangolare", "Chicago"])
        self.cmb_rain_type.setToolTip('Rectangular = constant average intensity computed from IDF h=a·t^n. Chicago = Keifer-Chu from IDF, with internal peak and before/after peak formulas.')
        row_rain_type.addWidget(self.cmb_rain_type)
        row_rain_type.addWidget(QLabel("Intervallo temporale [min]:"))
        self.spn_rain_step = QDoubleSpinBox()
        self.spn_rain_step.setRange(1, 60)
        self.spn_rain_step.setDecimals(0)
        self.spn_rain_step.setValue(5)
        self.spn_rain_step.setToolTip('Time interval used to discretize the rainfall in the SWMM TIMESERIES.')
        row_rain_type.addWidget(self.spn_rain_step)
        l_rain_box.addLayout(row_rain_type)

        row_rain = QHBoxLayout()
        row_rain.addWidget(QLabel("Rectangular: i=h/D from IDF/LSPP"))
        self.spn_rain = QDoubleSpinBox()
        self.spn_rain.setRange(0, 500)
        self.spn_rain.setDecimals(1)
        self.spn_rain.setValue(50.0)
        self.spn_rain.setEnabled(False)
        self.spn_rain.setToolTip('Value no longer entered manually: the rectangular rainfall is computed with h=a·t^n and i=h/D.')
        row_rain.addWidget(self.spn_rain)
        row_rain.addWidget(QLabel("Durata evento [min]:"))
        self.spn_duration = QDoubleSpinBox()
        self.spn_duration.setRange(5, 1440)
        self.spn_duration.setDecimals(0)
        self.spn_duration.setValue(60)
        row_rain.addWidget(self.spn_duration)
        l_rain_box.addLayout(row_rain)

        box_chicago = QGroupBox("Chicago from IDF/LSPP")
        l_chicago = QGridLayout(box_chicago)
        l_chicago.addWidget(QLabel("a LSPP [mm]"), 0, 0)
        self.spn_ch_a = QDoubleSpinBox()
        self.spn_ch_a.setRange(0.001, 10000.0)
        self.spn_ch_a.setDecimals(3)
        self.spn_ch_a.setValue(46.0)
        self.spn_ch_a.setToolTip('Parameter a of the IDF curve h = a · t^n. With t in hours, h is in mm. Used for both rectangular and Chicago storms.')
        l_chicago.addWidget(self.spn_ch_a, 0, 1)
        l_chicago.addWidget(QLabel("TR [anni]"), 0, 2)
        self.spn_ch_b = QDoubleSpinBox()
        self.spn_ch_b.setRange(1.0, 500.0)
        self.spn_ch_b.setDecimals(0)
        self.spn_ch_b.setValue(25.0)
        self.spn_ch_b.setToolTip('Descriptive return period; it is not used in the formula if a and n already refer to the selected return period.')
        l_chicago.addWidget(self.spn_ch_b, 0, 3)
        l_chicago.addWidget(QLabel("n LSPP [-]"), 1, 0)
        self.spn_ch_n = QDoubleSpinBox()
        self.spn_ch_n.setRange(0.001, 0.999)
        self.spn_ch_n.setDecimals(3)
        self.spn_ch_n.setSingleStep(0.01)
        self.spn_ch_n.setValue(0.487)
        l_chicago.addWidget(self.spn_ch_n, 1, 1)
        l_chicago.addWidget(QLabel("Picco r [-]"), 1, 2)
        self.spn_ch_r = QDoubleSpinBox()
        self.spn_ch_r.setRange(0.05, 0.95)
        self.spn_ch_r.setDecimals(2)
        self.spn_ch_r.setSingleStep(0.05)
        self.spn_ch_r.setValue(0.40)
        self.spn_ch_r.setToolTip("r=0.40 means the peak occurs at 40% of the event duration.")
        l_chicago.addWidget(self.spn_ch_r, 1, 3)
        lbl_ch_note = QLabel('Rectangular: h=a·D^n, i=h/D. Chicago Keifer-Chu: i_b=n·a·(θ_b/r)^(n-1), i_a=n·a·(θ_a/(1-r))^(n-1). The TIMESERIES always starts from t=0 with zero rainfall.')
        lbl_ch_note.setWordWrap(True)
        l_chicago.addWidget(lbl_ch_note, 2, 0, 1, 4)
        l_rain_box.addWidget(box_chicago)

        row_rain_preview = QHBoxLayout()
        self.btn_rain_preview = QPushButton("Show hyetograph chart")
        self.btn_rain_preview.setToolTip("Show the rainfall hyetograph generated with the current parameters and selected time interval.")
        self.btn_rain_preview.clicked.connect(self.show_rain_graph)
        row_rain_preview.addWidget(self.btn_rain_preview)
        self.lbl_rain_preview_note = QLabel('The chart uses the same time interval that will be written in [TIMESERIES].')
        self.lbl_rain_preview_note.setWordWrap(True)
        row_rain_preview.addWidget(self.lbl_rain_preview_note)
        l_rain_box.addLayout(row_rain_preview)
        rain_split.addWidget(box_rain, 2)

        l_params.addLayout(rain_split)

        row_rough = QHBoxLayout()
        row_rough.addWidget(QLabel('Roughness Manning n conduits:'))
        self.spn_roughness = QDoubleSpinBox()
        self.spn_roughness.setRange(0.001, 0.100)
        self.spn_roughness.setDecimals(5)
        self.spn_roughness.setSingleStep(0.001)
        self.spn_roughness.setValue(0.01300)
        row_rough.addWidget(self.spn_roughness)
        l_params.addLayout(row_rough)

        tab_swmm.addWidget(box_params)
        tab_swmm.addStretch(1)

        box_out = QGroupBox("Output and simulation")
        l_out = QVBoxLayout(box_out)
        row_outfall = QHBoxLayout()
        row_outfall.addWidget(QLabel('Final outfall node:'))
        self.txt_outfall = QLineEdit("")
        self.txt_outfall.setPlaceholderText("e.g. N123 or terminal manhole ID")
        row_outfall.addWidget(self.txt_outfall)
        l_out.addLayout(row_outfall)

        row_outfall_stage = QHBoxLayout()
        row_outfall_stage.addWidget(QLabel('Fixed outfall stage [absolute elevation m]:'))
        self.txt_outfall_fixed_stage = QLineEdit("")
        self.txt_outfall_fixed_stage.setPlaceholderText("Lascia vuoto = FREE; es. 28.35 = FIXED")
        self.txt_outfall_fixed_stage.setToolTip(
            "If you enter an absolute water level elevation, the outfall is written as FIXED. "
            "If left empty, the outfall remains FREE."
        )
        row_outfall_stage.addWidget(self.txt_outfall_fixed_stage)
        l_out.addLayout(row_outfall_stage)

        note_outfall_stage = QLabel(
            'Boundary condition: empty = FREE outfall; elevation filled in = FIXED outfall with assigned water level.'
        )
        note_outfall_stage.setWordWrap(True)
        l_out.addWidget(note_outfall_stage)

        box_manual_swmm = QGroupBox('Final manual additions: nodes, outfalls and conduits')
        l_manual_swmm = QVBoxLayout(box_manual_swmm)
        note_manual_swmm = QLabel(
            'Optional: add new SWMM junctions, outfalls and conduits at the end of the model. '
            "Elements can be edited manually in the tables and are written to the INP as if they were already present in the Sewer Builder inputs."
        )
        note_manual_swmm.setWordWrap(True)
        l_manual_swmm.addWidget(note_manual_swmm)

        self.tbl_extra_nodes = QTableWidget(0, 6)
        self.tbl_extra_nodes.setHorizontalHeaderLabels(["Node ID", "Ground elev.", "Invert elev.", "MaxDepth", "X", "Y"])
        self.tbl_extra_nodes.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_extra_nodes.setMinimumHeight(105)
        l_manual_swmm.addWidget(QLabel('New nodes / junctions'))
        l_manual_swmm.addWidget(self.tbl_extra_nodes)
        row_extra_nodes = QHBoxLayout()
        self.btn_draw_extra_node = QPushButton('Draw new node')
        self.btn_draw_extra_node.clicked.connect(lambda: self.start_draw_extra_swmm_node("node"))
        row_extra_nodes.addWidget(self.btn_draw_extra_node)
        self.btn_add_extra_node_row = QPushButton('Add node from table')
        self.btn_add_extra_node_row.clicked.connect(lambda: self.add_extra_node_row(kind="node"))
        row_extra_nodes.addWidget(self.btn_add_extra_node_row)
        self.btn_remove_extra_node = QPushButton('Remove selected node')
        self.btn_remove_extra_node.clicked.connect(lambda: self.remove_selected_extra_row(self.tbl_extra_nodes))
        row_extra_nodes.addWidget(self.btn_remove_extra_node)
        l_manual_swmm.addLayout(row_extra_nodes)

        self.tbl_extra_outfalls = QTableWidget(0, 6)
        self.tbl_extra_outfalls.setHorizontalHeaderLabels(["Outfall ID", "Invert elev.", "Fixed stage", "Gated", "X", "Y"])
        self.tbl_extra_outfalls.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_extra_outfalls.setMinimumHeight(105)
        l_manual_swmm.addWidget(QLabel("New outfalls"))
        l_manual_swmm.addWidget(self.tbl_extra_outfalls)
        row_extra_outfalls = QHBoxLayout()
        self.btn_draw_extra_outfall = QPushButton('Draw new outfall')
        self.btn_draw_extra_outfall.clicked.connect(lambda: self.start_draw_extra_swmm_node("outfall"))
        row_extra_outfalls.addWidget(self.btn_draw_extra_outfall)
        self.btn_add_extra_outfall_row = QPushButton('Add outfall from table')
        self.btn_add_extra_outfall_row.clicked.connect(lambda: self.add_extra_node_row(kind="outfall"))
        row_extra_outfalls.addWidget(self.btn_add_extra_outfall_row)
        self.btn_remove_extra_outfall = QPushButton("Remove selected outfall")
        self.btn_remove_extra_outfall.clicked.connect(lambda: self.remove_selected_extra_row(self.tbl_extra_outfalls))
        row_extra_outfalls.addWidget(self.btn_remove_extra_outfall)
        l_manual_swmm.addLayout(row_extra_outfalls)

        self.tbl_extra_links = QTableWidget(0, 11)
        self.tbl_extra_links.setHorizontalHeaderLabels(["Link ID", "From", "To", "Shape", "Geom1 / H / D [m]", "Geom2 / W [m]", "Roughness", "InOffset", "OutOffset", "Length [m]", "Source"])
        self.tbl_extra_links.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_extra_links.setMinimumHeight(115)
        l_manual_swmm.addWidget(QLabel('New conduits / links between two existing nodes'))
        row_extra_link_shape = QHBoxLayout()
        row_extra_link_shape.addWidget(QLabel('New conduit shape:'))
        self.cmb_extra_link_shape = QComboBox()
        self.cmb_extra_link_shape.addItems(["CIRCULAR", "RECT_CLOSED", "EGG", "ARCH"])
        row_extra_link_shape.addWidget(self.cmb_extra_link_shape)
        row_extra_link_shape.addWidget(QLabel('Geom1 / altezza-diameter [m]:'))
        self.spn_extra_link_geom1 = QDoubleSpinBox()
        self.spn_extra_link_geom1.setRange(0.001, 100.0)
        self.spn_extra_link_geom1.setDecimals(3)
        self.spn_extra_link_geom1.setValue(0.300)
        row_extra_link_shape.addWidget(self.spn_extra_link_geom1)
        row_extra_link_shape.addWidget(QLabel("Geom2 / larghezza [m]:"))
        self.spn_extra_link_geom2 = QDoubleSpinBox()
        self.spn_extra_link_geom2.setRange(0.0, 100.0)
        self.spn_extra_link_geom2.setDecimals(3)
        self.spn_extra_link_geom2.setValue(0.000)
        row_extra_link_shape.addWidget(self.spn_extra_link_geom2)
        l_manual_swmm.addLayout(row_extra_link_shape)
        l_manual_swmm.addWidget(self.tbl_extra_links)
        row_extra_links = QHBoxLayout()
        self.btn_draw_extra_link = QPushButton('Draw new conduit between two nodes')
        self.btn_draw_extra_link.clicked.connect(self.start_draw_extra_conduit_link)
        row_extra_links.addWidget(self.btn_draw_extra_link)
        self.btn_add_extra_link_row = QPushButton('Add conduit from table')
        self.btn_add_extra_link_row.clicked.connect(self.add_extra_link_row)
        row_extra_links.addWidget(self.btn_add_extra_link_row)
        self.btn_remove_extra_link = QPushButton('Remove selected conduit')
        self.btn_remove_extra_link.clicked.connect(self.remove_selected_extra_link)
        row_extra_links.addWidget(self.btn_remove_extra_link)
        l_manual_swmm.addLayout(row_extra_links)
        note_extra_link = QLabel('For drawn conduits, you can click intermediate vertices before selecting the downstream node; vertices are written to [VERTICES]. For RECT_CLOSED use Geom1 = height and Geom2 = width; for CIRCULAR/EGG/ARCH mainly use Geom1.')
        note_extra_link.setWordWrap(True)
        l_manual_swmm.addWidget(note_extra_link)

        l_manual_swmm.addWidget(QLabel('Edit shape/dimensions of existing conduits'))
        self.tbl_conduit_shape_overrides = QTableWidget(0, 4)
        self.tbl_conduit_shape_overrides.setHorizontalHeaderLabels(['Conduit ID', "Shape", "Geom1 / H / D [m]", "Geom2 / W [m]"])
        self.tbl_conduit_shape_overrides.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_conduit_shape_overrides.setMinimumHeight(105)
        l_manual_swmm.addWidget(self.tbl_conduit_shape_overrides)
        row_shape_override = QHBoxLayout()
        self.btn_add_conduit_shape_override = QPushButton('Add shape edit')
        self.btn_add_conduit_shape_override.clicked.connect(self.add_conduit_shape_override_row)
        row_shape_override.addWidget(self.btn_add_conduit_shape_override)
        self.btn_add_selected_conduit_shape_override = QPushButton('Add selected conduits')
        self.btn_add_selected_conduit_shape_override.clicked.connect(self.add_selected_conduit_shape_overrides)
        row_shape_override.addWidget(self.btn_add_selected_conduit_shape_override)
        self.btn_remove_conduit_shape_override = QPushButton("Remove selected edit")
        self.btn_remove_conduit_shape_override.clicked.connect(lambda: self.remove_selected_extra_row(self.tbl_conduit_shape_overrides))
        row_shape_override.addWidget(self.btn_remove_conduit_shape_override)
        l_manual_swmm.addLayout(row_shape_override)
        note_shape_override = QLabel('The edit affects only the [XSECTIONS] section of the INP file: conduit geometry, upstream/downstream nodes, length, roughness and offsets remain unchanged.')
        note_shape_override.setWordWrap(True)
        l_manual_swmm.addWidget(note_shape_override)

        l_out.addWidget(box_manual_swmm)
        self.make_group_collapsible(box_manual_swmm, checked=False)

        box_storage = QGroupBox("Storage node")
        l_storage = QVBoxLayout(box_storage)
        note_storage = QLabel(
            "Optional: enter the ID of a node to convert into a storage node. "
            'The selected node will be written to the [STORAGE] section instead of [JUNCTIONS].'
        )
        note_storage.setWordWrap(True)
        l_storage.addWidget(note_storage)

        row_storage_node = QHBoxLayout()
        row_storage_node.addWidget(QLabel('Storage node:'))
        self.txt_storage_node = QLineEdit("")
        self.txt_storage_node.setPlaceholderText('e.g. N45; leave blank = no storage')
        row_storage_node.addWidget(self.txt_storage_node)
        l_storage.addLayout(row_storage_node)

        self.tbl_storage_curve = QTableWidget(0, 2)
        self.tbl_storage_curve.setHorizontalHeaderLabels(["Depth [m]", "Area [m²]"])
        self.tbl_storage_curve.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_storage_curve.setMinimumHeight(130)
        l_storage.addWidget(self.tbl_storage_curve)

        row_storage_buttons = QHBoxLayout()
        self.btn_storage_add_row = QPushButton('Add curve point')
        self.btn_storage_add_row.clicked.connect(self.add_storage_curve_row)
        row_storage_buttons.addWidget(self.btn_storage_add_row)
        self.btn_storage_remove_row = QPushButton("Remove selected point")
        self.btn_storage_remove_row.clicked.connect(self.remove_storage_curve_row)
        row_storage_buttons.addWidget(self.btn_storage_remove_row)
        self.btn_storage_preview_curve = QPushButton('Preview storage chart')
        self.btn_storage_preview_curve.clicked.connect(self.show_storage_curve_preview)
        row_storage_buttons.addWidget(self.btn_storage_preview_curve)
        l_storage.addLayout(row_storage_buttons)

        note_storage_curve = QLabel(
            "Storage curve editor: inserisci coppie Depth/Area. "
            'The curve must contain at least two points; starting from depth = 0 is recommended.'
        )
        note_storage_curve.setWordWrap(True)
        l_storage.addWidget(note_storage_curve)
        l_out.addWidget(box_storage)
        self.make_group_collapsible(box_storage, checked=False)

        box_orifice = QGroupBox("Orifice link")
        l_orifice = QVBoxLayout(box_orifice)
        note_orifice = QLabel(
            "Optional: enter the ID of a conduit to convert into an orifice. "
            'The selected conduit will be removed from [CONDUITS] and written to [ORIFICES].'
        )
        note_orifice.setWordWrap(True)
        l_orifice.addWidget(note_orifice)

        row_orifice_id = QHBoxLayout()
        row_orifice_id.addWidget(QLabel('Conduit to convert:'))
        self.txt_orifice_link = QLineEdit("")
        self.txt_orifice_link.setPlaceholderText('e.g. C_12; leave blank = no orifice')
        row_orifice_id.addWidget(self.txt_orifice_link)
        l_orifice.addLayout(row_orifice_id)

        row_orifice_type = QHBoxLayout()
        row_orifice_type.addWidget(QLabel("Type:"))
        self.cmb_orifice_type = QComboBox()
        self.cmb_orifice_type.addItems(["SIDE", "BOTTOM"])
        row_orifice_type.addWidget(self.cmb_orifice_type)
        row_orifice_type.addWidget(QLabel("Shape:"))
        self.cmb_orifice_shape = QComboBox()
        self.cmb_orifice_shape.addItems(["CIRCULAR", "RECT_CLOSED"])
        row_orifice_type.addWidget(self.cmb_orifice_shape)
        l_orifice.addLayout(row_orifice_type)

        row_orifice_geom = QHBoxLayout()
        row_orifice_geom.addWidget(QLabel("Height / diameter [m]:"))
        self.spn_orifice_height = QDoubleSpinBox()
        self.spn_orifice_height.setRange(0.001, 100.0)
        self.spn_orifice_height.setDecimals(3)
        self.spn_orifice_height.setValue(0.300)
        row_orifice_geom.addWidget(self.spn_orifice_height)
        row_orifice_geom.addWidget(QLabel("Width [m]:"))
        self.spn_orifice_width = QDoubleSpinBox()
        self.spn_orifice_width.setRange(0.001, 100.0)
        self.spn_orifice_width.setDecimals(3)
        self.spn_orifice_width.setValue(0.300)
        row_orifice_geom.addWidget(self.spn_orifice_width)
        l_orifice.addLayout(row_orifice_geom)

        row_orifice_offset = QHBoxLayout()
        row_orifice_offset.addWidget(QLabel("Inlet offset [m]:"))
        self.spn_orifice_offset = QDoubleSpinBox()
        self.spn_orifice_offset.setRange(0.0, 1000.0)
        self.spn_orifice_offset.setDecimals(3)
        self.spn_orifice_offset.setSingleStep(0.05)
        self.spn_orifice_offset.setValue(0.000)
        self.spn_orifice_offset.setToolTip('Height of the orifice centroid/crest above the invert of the upstream manhole/node.')
        row_orifice_offset.addWidget(self.spn_orifice_offset)
        row_orifice_offset.addStretch(1)
        l_orifice.addLayout(row_orifice_offset)

        row_orifice_coeff = QHBoxLayout()
        row_orifice_coeff.addWidget(QLabel("Discharge coeff.:"))
        self.spn_orifice_coeff = QDoubleSpinBox()
        self.spn_orifice_coeff.setRange(0.01, 5.0)
        self.spn_orifice_coeff.setDecimals(3)
        self.spn_orifice_coeff.setSingleStep(0.01)
        self.spn_orifice_coeff.setValue(0.650)
        row_orifice_coeff.addWidget(self.spn_orifice_coeff)
        l_orifice.addLayout(row_orifice_coeff)

        note_rules = QLabel(
            "Optional control rules: write complete SWMM rules, for example "
            "RULE R1 / IF NODE N1 DEPTH > 1.00 / THEN ORIFICE O1 SETTING = 0.50."
        )
        note_rules.setWordWrap(True)
        l_orifice.addWidget(note_rules)
        self.txt_control_rules = QTextEdit()
        self.txt_control_rules.setPlaceholderText(
            "RULE GATE_1\n"
            "IF NODE N1 DEPTH > 1.00\n"
            "THEN ORIFICE C_12 SETTING = 1.00\n"
            "ELSE ORIFICE C_12 SETTING = 0.25"
        )
        self.txt_control_rules.setMinimumHeight(110)
        l_orifice.addWidget(self.txt_control_rules)

        row_orifice_buttons = QHBoxLayout()
        self.btn_add_orifice_from_conduit = QPushButton('Add orifice from conduit')
        self.btn_add_orifice_from_conduit.clicked.connect(self.add_orifice_from_conduit)
        row_orifice_buttons.addWidget(self.btn_add_orifice_from_conduit)
        self.btn_draw_orifice = QPushButton('Draw orifice between two nodes')
        self.btn_draw_orifice.clicked.connect(self.start_draw_orifice_link)
        row_orifice_buttons.addWidget(self.btn_draw_orifice)
        self.btn_remove_orifice = QPushButton('Remove selected orifice')
        self.btn_remove_orifice.clicked.connect(self.remove_selected_orifice)
        row_orifice_buttons.addWidget(self.btn_remove_orifice)
        l_orifice.addLayout(row_orifice_buttons)

        self.tbl_orifices = QTableWidget(0, 9)
        self.tbl_orifices.setHorizontalHeaderLabels(["Orifice ID", "From", "To", "Type", "Shape", "Offset", "Height", "Width", "Source"])
        self.tbl_orifices.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_orifices.setMinimumHeight(110)
        l_orifice.addWidget(self.tbl_orifices)
        l_out.addWidget(box_orifice)
        self.make_group_collapsible(box_orifice, checked=False)

        box_weirs = QGroupBox("Weir links")
        l_weirs = QVBoxLayout(box_weirs)
        note_weirs = QLabel(
            "Optional: enter the ID of a conduit to convert into a weir, or draw a new weir between two existing nodes. "
            "The link will be written to [WEIRS]; the main dimensions are reported in [XSECTIONS]."
        )
        note_weirs.setWordWrap(True)
        l_weirs.addWidget(note_weirs)

        row_weir_source = QHBoxLayout()
        row_weir_source.addWidget(QLabel('Conduit to convert:'))
        self.txt_weir_link = QLineEdit("")
        self.txt_weir_link.setPlaceholderText("e.g. C_30; or use 'Draw weir'")
        row_weir_source.addWidget(self.txt_weir_link)
        l_weirs.addLayout(row_weir_source)

        row_weir_type = QHBoxLayout()
        row_weir_type.addWidget(QLabel("Type:"))
        self.cmb_weir_type = QComboBox()
        self.cmb_weir_type.addItems(["TRANSVERSE", "SIDEFLOW", "V-NOTCH", "TRAPEZOIDAL"])
        row_weir_type.addWidget(self.cmb_weir_type)
        row_weir_type.addWidget(QLabel("Shape:"))
        self.cmb_weir_shape = QComboBox()
        self.cmb_weir_shape.addItems(["RECT_OPEN", "TRAPEZOIDAL", "TRIANGULAR"])
        row_weir_type.addWidget(self.cmb_weir_shape)
        l_weirs.addLayout(row_weir_type)

        row_weir_geom = QHBoxLayout()
        row_weir_geom.addWidget(QLabel("Opening height [m]:"))
        self.spn_weir_height = QDoubleSpinBox()
        self.spn_weir_height.setRange(0.0, 1000.0)
        self.spn_weir_height.setDecimals(3)
        self.spn_weir_height.setValue(0.300)
        row_weir_geom.addWidget(self.spn_weir_height)
        row_weir_geom.addWidget(QLabel("Width / crest length [m]:"))
        self.spn_weir_width = QDoubleSpinBox()
        self.spn_weir_width.setRange(0.001, 1000.0)
        self.spn_weir_width.setDecimals(3)
        self.spn_weir_width.setValue(1.000)
        row_weir_geom.addWidget(self.spn_weir_width)
        l_weirs.addLayout(row_weir_geom)

        row_weir_offset = QHBoxLayout()
        row_weir_offset.addWidget(QLabel("Inlet offset / crest height [m]:"))
        self.spn_weir_offset = QDoubleSpinBox()
        self.spn_weir_offset.setRange(0.0, 1000.0)
        self.spn_weir_offset.setDecimals(3)
        self.spn_weir_offset.setSingleStep(0.05)
        self.spn_weir_offset.setValue(0.000)
        self.spn_weir_offset.setToolTip('Weir crest elevation relative to the upstream node/manhole invert. It is written as CrestHt in [WEIRS].')
        row_weir_offset.addWidget(self.spn_weir_offset)
        row_weir_offset.addStretch(1)
        l_weirs.addLayout(row_weir_offset)

        row_weir_coeff = QHBoxLayout()
        row_weir_coeff.addWidget(QLabel("Discharge coeff.:"))
        self.spn_weir_coeff = QDoubleSpinBox()
        self.spn_weir_coeff.setRange(0.01, 20.0)
        self.spn_weir_coeff.setDecimals(3)
        self.spn_weir_coeff.setValue(1.700)
        row_weir_coeff.addWidget(self.spn_weir_coeff)
        row_weir_coeff.addWidget(QLabel("End contractions:"))
        self.spn_weir_endcon = QDoubleSpinBox()
        self.spn_weir_endcon.setRange(0.0, 2.0)
        self.spn_weir_endcon.setDecimals(0)
        self.spn_weir_endcon.setValue(0.0)
        row_weir_coeff.addWidget(self.spn_weir_endcon)
        l_weirs.addLayout(row_weir_coeff)

        note_weir_rules = QLabel(
            "Optional weir control rules: write complete SWMM rules, for example "
            "IF NODE N1 DEPTH > 1.00 THEN WEIR W1 SETTING = 1.00."
        )
        note_weir_rules.setWordWrap(True)
        l_weirs.addWidget(note_weir_rules)
        self.txt_weir_control_rules = QTextEdit()
        self.txt_weir_control_rules.setPlaceholderText(
            "RULE WEIR_1\n"
            "IF NODE N1 DEPTH > 1.00\n"
            "THEN WEIR W1 SETTING = 1.00\n"
            "ELSE WEIR W1 SETTING = 0.25"
        )
        self.txt_weir_control_rules.setMinimumHeight(100)
        l_weirs.addWidget(self.txt_weir_control_rules)

        row_weir_buttons = QHBoxLayout()
        self.btn_add_weir_from_conduit = QPushButton('Add weir from conduit')
        self.btn_add_weir_from_conduit.clicked.connect(self.add_weir_from_conduit)
        row_weir_buttons.addWidget(self.btn_add_weir_from_conduit)
        self.btn_draw_weir = QPushButton('Draw weir between two nodes')
        self.btn_draw_weir.clicked.connect(self.start_draw_weir_link)
        row_weir_buttons.addWidget(self.btn_draw_weir)
        self.btn_remove_weir = QPushButton("Remove selected weir")
        self.btn_remove_weir.clicked.connect(self.remove_selected_weir)
        row_weir_buttons.addWidget(self.btn_remove_weir)
        l_weirs.addLayout(row_weir_buttons)

        self.tbl_weirs = QTableWidget(0, 9)
        self.tbl_weirs.setHorizontalHeaderLabels(["Weir ID", "From", "To", "Type", "Shape", "Offset/Crest", "Height", "Width", "Source"])
        self.tbl_weirs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_weirs.setMinimumHeight(110)
        l_weirs.addWidget(self.tbl_weirs)
        l_out.addWidget(box_weirs)
        self.make_group_collapsible(box_weirs, checked=False)

        box_pumps = QGroupBox("Pump links")
        l_pumps = QVBoxLayout(box_pumps)
        note_pumps = QLabel(
            'You can convert an existing conduit into a pump or draw a new pump between two existing nodes. '
            "Pumps will be written to [PUMPS] and the curve to [CURVES]."
        )
        note_pumps.setWordWrap(True)
        l_pumps.addWidget(note_pumps)

        row_pump_source = QHBoxLayout()
        row_pump_source.addWidget(QLabel('Conduit to convert:'))
        self.txt_pump_link = QLineEdit("")
        self.txt_pump_link.setPlaceholderText("e.g. C_20; or use 'Draw pump'")
        row_pump_source.addWidget(self.txt_pump_link)
        self.btn_add_pump_from_conduit = QPushButton('Add pump from conduit')
        self.btn_add_pump_from_conduit.clicked.connect(self.add_pump_from_conduit)
        row_pump_source.addWidget(self.btn_add_pump_from_conduit)
        l_pumps.addLayout(row_pump_source)

        row_pump_params = QHBoxLayout()
        row_pump_params.addWidget(QLabel("Curve name:"))
        self.txt_pump_curve_name = QLineEdit("PUMP_CURVE")
        row_pump_params.addWidget(self.txt_pump_curve_name)
        row_pump_params.addWidget(QLabel("Curve type:"))
        self.cmb_pump_curve_type = QComboBox()
        self.cmb_pump_curve_type.addItems(["PUMP3", "PUMP1", "PUMP2", "PUMP4"])
        row_pump_params.addWidget(self.cmb_pump_curve_type)
        row_pump_params.addWidget(QLabel("Status:"))
        self.cmb_pump_status = QComboBox()
        self.cmb_pump_status.addItems(["ON", "OFF"])
        row_pump_params.addWidget(self.cmb_pump_status)
        l_pumps.addLayout(row_pump_params)

        row_pump_depths = QHBoxLayout()
        row_pump_depths.addWidget(QLabel("Startup depth [m]:"))
        self.spn_pump_startup = QDoubleSpinBox()
        self.spn_pump_startup.setRange(0.0, 1000.0)
        self.spn_pump_startup.setDecimals(3)
        self.spn_pump_startup.setValue(1.000)
        row_pump_depths.addWidget(self.spn_pump_startup)
        row_pump_depths.addWidget(QLabel("Shutoff depth [m]:"))
        self.spn_pump_shutoff = QDoubleSpinBox()
        self.spn_pump_shutoff.setRange(0.0, 1000.0)
        self.spn_pump_shutoff.setDecimals(3)
        self.spn_pump_shutoff.setValue(0.200)
        row_pump_depths.addWidget(self.spn_pump_shutoff)
        l_pumps.addLayout(row_pump_depths)

        row_pump_buttons = QHBoxLayout()
        self.btn_draw_pump = QPushButton('Draw pump between two nodes')
        self.btn_draw_pump.clicked.connect(self.start_draw_pump_link)
        row_pump_buttons.addWidget(self.btn_draw_pump)
        self.btn_remove_pump = QPushButton('Remove selected pump')
        self.btn_remove_pump.clicked.connect(self.remove_selected_pump)
        row_pump_buttons.addWidget(self.btn_remove_pump)
        l_pumps.addLayout(row_pump_buttons)

        self.tbl_pumps = QTableWidget(0, 9)
        self.tbl_pumps.setHorizontalHeaderLabels(["Pump ID", "From", "To", "Curve", "Type", "Startup", "Shutoff", "Status", "Source"])
        self.tbl_pumps.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_pumps.setMinimumHeight(120)
        l_pumps.addWidget(self.tbl_pumps)

        self.lbl_pump_curve_note = QLabel(
            'Pump curve editor: choose the curve type. The plugin enables only the columns required by SWMM '
            "and disables non-applicable fields."
        )
        self.lbl_pump_curve_note.setWordWrap(True)
        l_pumps.addWidget(self.lbl_pump_curve_note)
        self.tbl_pump_curve = QTableWidget(0, 4)
        self.tbl_pump_curve.setHorizontalHeaderLabels(["Volume [m³]", "Depth [m]", "Head [m]", "Flow [L/s]"])
        self.tbl_pump_curve.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_pump_curve.setMinimumHeight(120)
        l_pumps.addWidget(self.tbl_pump_curve)
        self.cmb_pump_curve_type.currentTextChanged.connect(self.update_pump_curve_editor)
        self.update_pump_curve_editor()

        row_pump_curve_buttons = QHBoxLayout()
        self.btn_add_pump_curve_row = QPushButton('Add pump curve point')
        self.btn_add_pump_curve_row.clicked.connect(self.add_pump_curve_row)
        row_pump_curve_buttons.addWidget(self.btn_add_pump_curve_row)
        self.btn_remove_pump_curve_row = QPushButton('Remove curve point')
        self.btn_remove_pump_curve_row.clicked.connect(self.remove_pump_curve_row)
        row_pump_curve_buttons.addWidget(self.btn_remove_pump_curve_row)
        self.btn_pump_preview_curve = QPushButton('Preview pump curve chart')
        self.btn_pump_preview_curve.clicked.connect(self.show_pump_curve_preview)
        row_pump_curve_buttons.addWidget(self.btn_pump_preview_curve)
        l_pumps.addLayout(row_pump_curve_buttons)

        note_pump_rules = QLabel(
            "Optional pump control rules: you can define ON/OFF logic based on water level changes, "
            "ad esempio IF NODE S1 DEPTH > 1.20 THEN PUMP P1 STATUS = ON."
        )
        note_pump_rules.setWordWrap(True)
        l_pumps.addWidget(note_pump_rules)
        self.txt_pump_control_rules = QTextEdit()
        self.txt_pump_control_rules.setPlaceholderText(
            "RULE PUMP_1_ON\n"
            "IF NODE S1 DEPTH > 1.20\n"
            "THEN PUMP P1 STATUS = ON\n"
            "ELSE PUMP P1 STATUS = OFF"
        )
        self.txt_pump_control_rules.setMinimumHeight(100)
        l_pumps.addWidget(self.txt_pump_control_rules)
        l_out.addWidget(box_pumps)
        self.make_group_collapsible(box_pumps, checked=False)

        row_out_note = QHBoxLayout()
        row_out_note.addWidget(QLabel('Output folder set in tab 1:'))
        self.lbl_outdir_ref = QLabel(self.txt_outdir.text())
        self.lbl_outdir_ref.setWordWrap(True)
        self.txt_outdir.textChanged.connect(self.lbl_outdir_ref.setText)
        row_out_note.addWidget(self.lbl_outdir_ref)
        l_out.addLayout(row_out_note)

        note_py = QLabel(
            "The simulation is run using the internal QGIS Python environment. "
            'swmm-toolkit must be installed in the QGIS Python environment.'
        )
        note_py.setWordWrap(True)
        l_out.addWidget(note_py)

        self.chk_run = QCheckBox('Run SWMM simulation after INP creation')
        self.chk_run.setChecked(True)
        l_out.addWidget(self.chk_run)

        self.chk_load_results = QCheckBox('Load maximum results into the QGIS project')
        self.chk_load_results.setChecked(True)
        l_out.addWidget(self.chk_load_results)
        self.btn_prepare_inputs = QPushButton('Prepare SWMM input: merge manholes and conduits')
        self.btn_prepare_inputs.setToolTip('Opens a dialog to merge all manhole_calculated and pipe_calculated layers into two consolidated files to be used for SWMM model generation.')
        self.btn_prepare_inputs.clicked.connect(self.prepare_swmm_inputs_dialog)
        tab_output.addWidget(self.btn_prepare_inputs)

        # After preparing the merged files, the user selects the actual layers
        # to be used for INP model generation. This group remains above the
        # generation button to keep the workflow clear: prepare -> select inputs -> generate.
        tab_output.addWidget(box_layers)
        tab_output.addWidget(box_out)

        row_btn = QHBoxLayout()
        self.btn_generate = QPushButton("Generate SWMM model")
        self.btn_generate.clicked.connect(self.generate_model)
        row_btn.addWidget(self.btn_generate)
        self.btn_close = QPushButton('Close')
        self.btn_close.clicked.connect(self.close)
        row_btn.addWidget(self.btn_close)
        tab_output.addLayout(row_btn)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(230)
        tab_output.addWidget(self.log)
        tab_output.addStretch(1)

    def log_msg(self, msg):
        self.log.append(str(msg))

    def _set_layout_visible(self, layout, visible):
        if layout is None:
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            w = item.widget()
            child_layout = item.layout()
            if w is not None:
                w.setVisible(bool(visible))
            if child_layout is not None:
                self._set_layout_visible(child_layout, visible)

    def make_group_collapsible(self, group, checked=False):
        group.setCheckable(True)
        group.setChecked(bool(checked))

        def _toggle(visible, g=group):
            self._set_layout_visible(g.layout(), visible)

        group.toggled.connect(_toggle)
        _toggle(bool(checked))

    def _node_input_crs(self):
        layer = None
        try:
            layer = self.get_layer(self.cmb_nodi)
        except Exception:
            layer = None
        if layer is not None:
            return layer.crs()
        return self.canvas.mapSettings().destinationCrs()

    def _next_id_from_table(self, table, prefix):
        used = set()
        if table is not None:
            for r in range(table.rowCount()):
                item = table.item(r, 0)
                if item and item.text().strip():
                    used.add(item.text().strip())
        i = 1
        val = f"{prefix}{i}"
        while val in used:
            i += 1
            val = f"{prefix}{i}"
        return val

    def _set_table_row_values(self, table, values):
        row = table.rowCount()
        table.insertRow(row)
        for col, val in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(str(val)))
        return row

    def add_extra_node_row(self, kind="node", point=None):
        if kind == "outfall":
            table = self.tbl_extra_outfalls
            node_id = self._next_id_from_table(table, "OUT_")
            x = point.x() if point else 0.0
            y = point.y() if point else 0.0
            self._set_table_row_values(table, [node_id, "0.000", "", "NO", f"{x:.3f}", f"{y:.3f}"])
        else:
            table = self.tbl_extra_nodes
            node_id = self._next_id_from_table(table, "J_")
            x = point.x() if point else 0.0
            y = point.y() if point else 0.0
            self._set_table_row_values(table, [node_id, "0.000", "-2.000", "2.000", f"{x:.3f}", f"{y:.3f}"])

    def remove_selected_extra_row(self, table):
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            table.removeRow(row)

    def start_draw_extra_swmm_node(self, kind="node"):
        try:
            target_crs = self._node_input_crs()
            if self.extra_node_draw_tool:
                try:
                    self.extra_node_draw_tool.clear_drawing()
                except Exception:
                    pass
            label = "outfall" if kind == "outfall" else 'node'
            self.extra_node_draw_tool = ManualSWMMNodeDrawTool(
                self.canvas,
                target_crs,
                lambda point, crs: self.on_extra_swmm_node_drawn(kind, point, crs),
                self.log_msg,
                label,
            )
            self.canvas.setMapTool(self.extra_node_draw_tool)
            self.log_msg(f"Strumento {label} manuale attivo: clicca sulla mappa per aggiungere il punto.")
        except Exception as e:
            QMessageBox.critical(self, "Aggiunte manuali", str(e))

    def on_extra_swmm_node_drawn(self, kind, point, crs):
        self.add_extra_node_row(kind=kind, point=point)
        try:
            self.canvas.unsetMapTool(self.extra_node_draw_tool)
        except Exception:
            pass
        self.extra_node_draw_tool = None

    def _extra_node_snap_items(self):
        crs = self._node_input_crs()
        items = []
        for table in (getattr(self, "tbl_extra_nodes", None), getattr(self, "tbl_extra_outfalls", None)):
            if table is None:
                continue
            for r in range(table.rowCount()):
                try:
                    node_id = table.item(r, 0).text().strip() if table.item(r, 0) else ""
                    if not node_id:
                        continue
                    x_col = 4
                    y_col = 5
                    x = _to_float(table.item(r, x_col).text().replace(",", ".") if table.item(r, x_col) else "", None)
                    y = _to_float(table.item(r, y_col).text().replace(",", ".") if table.item(r, y_col) else "", None)
                    if x is None or y is None:
                        continue
                    items.append({"node_id": node_id, "x": float(x), "y": float(y), "crs": crs})
                except Exception:
                    pass
        return items

    def _current_extra_link_shape_values(self):
        shape = self.cmb_extra_link_shape.currentText().strip() if hasattr(self, "cmb_extra_link_shape") else "CIRCULAR"
        geom1 = float(self.spn_extra_link_geom1.value()) if hasattr(self, "spn_extra_link_geom1") else 0.300
        geom2 = float(self.spn_extra_link_geom2.value()) if hasattr(self, "spn_extra_link_geom2") else 0.000
        return self._normalize_xsection_values(shape, geom1, geom2)

    def add_extra_link_row(self):
        table = self.tbl_extra_links
        link_id = self._next_id_from_table(table, "C_MAN_")
        shape, geom1, geom2 = self._current_extra_link_shape_values()
        self.manual_link_definitions.append({"vertices": []})
        self._set_table_row_values(table, [link_id, "", "", shape, f"{geom1:.3f}", f"{geom2:.3f}", f"{self.spn_roughness.value():.5f}", "0.000", "0.000", "1.000", "manual"])

    def start_draw_extra_conduit_link(self):
        try:
            node_layer = self.get_layer(self.cmb_nodi)
            if node_layer is None:
                raise Exception('Select the SWMM input node layer before drawing a new conduit.')
            if self.manual_link_draw_tool:
                try:
                    self.manual_link_draw_tool.clear_drawing()
                except Exception:
                    pass
            snap_tol = float(self.spn_snap_tolerance.value()) if hasattr(self, "spn_snap_tolerance") else 2.0
            self.manual_link_draw_tool = PumpLinkDrawTool(
                self.canvas,
                node_layer,
                self.on_extra_conduit_link_drawn,
                self.log_msg,
                snap_tol,
                "condotta",
                self._extra_node_snap_items,
            )
            self.canvas.setMapTool(self.manual_link_draw_tool)
            self.log_msg('Manual conduit tool active: click the upstream node, any intermediate vertices, and finally the downstream node.')
        except Exception as e:
            QMessageBox.critical(self, 'Manual conduit', str(e))

    def on_extra_conduit_link_drawn(self, nodes, vertices_layer=None):
        try:
            if len(nodes) != 2:
                return
            vertices_layer = vertices_layer or [nodes[0]["point_layer"], nodes[1]["point_layer"]]
            verts = [(pt.x(), pt.y()) for pt in vertices_layer]
            length = 0.0
            for i in range(1, len(verts)):
                dx = verts[i][0] - verts[i-1][0]
                dy = verts[i][1] - verts[i-1][1]
                length += math.sqrt(dx*dx + dy*dy)
            table = self.tbl_extra_links
            link_id = self._next_id_from_table(table, "C_MAN_")
            self.manual_link_definitions.append({"vertices": verts})
            shape, geom1, geom2 = self._current_extra_link_shape_values()
            self._set_table_row_values(table, [
                link_id,
                nodes[0]["node_id"],
                nodes[1]["node_id"],
                shape,
                f"{geom1:.3f}",
                f"{geom2:.3f}",
                f"{self.spn_roughness.value():.5f}",
                "0.000",
                "0.000",
                f"{max(length, 0.1):.3f}",
                "drawn",
            ])
            try:
                self.canvas.unsetMapTool(self.manual_link_draw_tool)
            except Exception:
                pass
            self.manual_link_draw_tool = None
        except Exception as e:
            QMessageBox.critical(self, 'Manual conduit', str(e))

    def remove_selected_extra_link(self):
        table = getattr(self, "tbl_extra_links", None)
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            table.removeRow(row)
            if 0 <= row < len(self.manual_link_definitions):
                self.manual_link_definitions.pop(row)

    def _table_text(self, table, row, col, default=""):
        if table is None:
            return default
        try:
            w = table.cellWidget(row, col)
            if isinstance(w, QComboBox):
                txt = w.currentText().strip()
                return txt if txt else default
        except Exception:
            pass
        item = table.item(row, col) if table is not None else None
        txt = item.text().strip() if item else ""
        return txt if txt else default

    def apply_manual_network_additions(self, nodi, nodi_features, nodi_layer):
        """Add manually defined nodes, outfalls and conduits to the model loaded from layers."""
        crs = nodi_layer.crs()
        outfall_defs = []
        used_ids = set(nodi.keys())

        def add_node_to_model(node_id, x, y, quota_terr, quota_fondo, max_depth):
            if not node_id:
                raise Exception('A manual node/outfall has no ID value.')
            if node_id in used_ids:
                raise Exception(f"ID nodo/outfall manuale duplicato o già presente nel modello: {node_id}.")
            used_ids.add(node_id)
            f = QgsFeature(nodi_layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
            for fname, value in [
                ("node_id", node_id), ("id", node_id), ("ID", node_id),
                ("elevaz", quota_terr), ("quota_terr", quota_terr), ("ground_elevation", quota_terr),
                ("q_scorr", quota_fondo), ("quota_fondo", quota_fondo), ("invert", quota_fondo), ("invert_elevation", quota_fondo),
                ("prof_scav", max_depth), ("max_depth", max_depth), ("excavation_depth", max_depth),
            ]:
                try:
                    idx = f.fields().indexFromName(fname)
                    if idx >= 0:
                        f.setAttribute(idx, value)
                except Exception:
                    pass
            nodi[node_id] = {
                "node_id": node_id,
                "fid": -100000 - len(used_ids),
                "x": float(x),
                "y": float(y),
                "quota_terr": float(quota_terr),
                "quota_fondo": float(quota_fondo),
                "max_depth": float(max_depth),
                "feature": f,
                "manual": True,
            }
            nodi_features.append(f)

        table = getattr(self, "tbl_extra_nodes", None)
        if table is not None:
            for r in range(table.rowCount()):
                node_id = self._table_text(table, r, 0)
                if not node_id:
                    continue
                qterr = _to_float(self._table_text(table, r, 1, "0").replace(",", "."), None)
                qfond = _to_float(self._table_text(table, r, 2, "0").replace(",", "."), None)
                depth = _to_float(self._table_text(table, r, 3, "1").replace(",", "."), None)
                x = _to_float(self._table_text(table, r, 4, "").replace(",", "."), None)
                y = _to_float(self._table_text(table, r, 5, "").replace(",", "."), None)
                if None in (qterr, qfond, depth, x, y):
                    raise Exception(f"Manual node riga {r+1}: compila valori numerici validi.")
                add_node_to_model(str(node_id), x, y, qterr, qfond, max(float(depth), 0.0))

        table = getattr(self, "tbl_extra_outfalls", None)
        if table is not None:
            for r in range(table.rowCount()):
                node_id = self._table_text(table, r, 0)
                if not node_id:
                    continue
                invert = _to_float(self._table_text(table, r, 1, "0").replace(",", "."), None)
                stage_raw = self._table_text(table, r, 2, "").replace(",", ".")
                stage = _to_float(stage_raw, None) if stage_raw else None
                gated = (self._table_text(table, r, 3, "NO") or "NO").upper()
                if gated not in ("YES", "NO"):
                    gated = "NO"
                x = _to_float(self._table_text(table, r, 4, "").replace(",", "."), None)
                y = _to_float(self._table_text(table, r, 5, "").replace(",", "."), None)
                if None in (invert, x, y):
                    raise Exception(f"Outfall manuale riga {r+1}: compila valori numerici validi.")
                add_node_to_model(str(node_id), x, y, invert, invert, 0.0)
                outfall_defs.append({"node_id": str(node_id), "stage": stage, "gated": gated})

        return outfall_defs

    def export_manual_nodes_outfalls_to_gpkg(self, outdir, crs):
        """Save manually defined nodes and outfalls to a GeoPackage and load them into QGIS."""
        try:
            if not outdir:
                return None
            has_nodes = hasattr(self, "tbl_extra_nodes") and self.tbl_extra_nodes is not None and self.tbl_extra_nodes.rowCount() > 0
            has_outfalls = hasattr(self, "tbl_extra_outfalls") and self.tbl_extra_outfalls is not None and self.tbl_extra_outfalls.rowCount() > 0
            if not has_nodes and not has_outfalls:
                return None

            uri = "Point?crs={}".format(crs.authid() if crs and crs.isValid() else "EPSG:4326")
            mem = QgsVectorLayer(uri, "nodi_outfall_manuali_swmm", "memory")
            pr = mem.dataProvider()
            pr.addAttributes([
                QgsField("tipo", QVariant.String),
                QgsField("node_id", QVariant.String),
                QgsField("quota_terr", QVariant.Double),
                QgsField("q_scorr", QVariant.Double),
                QgsField("max_depth", QVariant.Double),
                QgsField("fixed_stage", QVariant.Double),
                QgsField("gated", QVariant.String),
                QgsField("x", QVariant.Double),
                QgsField("y", QVariant.Double),
            ])
            mem.updateFields()

            def add_feat(tipo, node_id, qterr, qfond, depth, stage, gated, x, y):
                f = QgsFeature(mem.fields())
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
                f.setAttributes([
                    str(tipo), str(node_id),
                    float(qterr) if qterr is not None else None,
                    float(qfond) if qfond is not None else None,
                    float(depth) if depth is not None else None,
                    float(stage) if stage is not None else None,
                    str(gated or ""), float(x), float(y),
                ])
                pr.addFeature(f)

            table = getattr(self, "tbl_extra_nodes", None)
            if table is not None:
                for r in range(table.rowCount()):
                    node_id = self._table_text(table, r, 0)
                    if not node_id:
                        continue
                    qterr = _to_float(self._table_text(table, r, 1, "0").replace(",", "."), None)
                    qfond = _to_float(self._table_text(table, r, 2, "0").replace(",", "."), None)
                    depth = _to_float(self._table_text(table, r, 3, "1").replace(",", "."), None)
                    x = _to_float(self._table_text(table, r, 4, "").replace(",", "."), None)
                    y = _to_float(self._table_text(table, r, 5, "").replace(",", "."), None)
                    if None in (qterr, qfond, depth, x, y):
                        continue
                    add_feat("JUNCTION", node_id, qterr, qfond, depth, None, "", x, y)

            table = getattr(self, "tbl_extra_outfalls", None)
            if table is not None:
                for r in range(table.rowCount()):
                    node_id = self._table_text(table, r, 0)
                    if not node_id:
                        continue
                    invert = _to_float(self._table_text(table, r, 1, "0").replace(",", "."), None)
                    stage_raw = self._table_text(table, r, 2, "").replace(",", ".")
                    stage = _to_float(stage_raw, None) if stage_raw else None
                    gated = (self._table_text(table, r, 3, "NO") or "NO").upper()
                    x = _to_float(self._table_text(table, r, 4, "").replace(",", "."), None)
                    y = _to_float(self._table_text(table, r, 5, "").replace(",", "."), None)
                    if None in (invert, x, y):
                        continue
                    add_feat("OUTFALL", node_id, invert, invert, 0.0, stage, gated, x, y)

            mem.updateExtents()
            if mem.featureCount() == 0:
                return None

            gpkg_path = os.path.join(outdir, "nodi_outfall_manuali_swmm.gpkg")
            try:
                if os.path.exists(gpkg_path):
                    os.remove(gpkg_path)
            except Exception:
                pass

            err = QgsVectorFileWriter.writeAsVectorFormat(mem, gpkg_path, "UTF-8", crs, "GPKG")
            # QGIS compatibility: in some versions err is a tuple, in others it is a code.
            if isinstance(err, tuple):
                err_code = err[0]
            else:
                err_code = err
            if err_code not in (0, QgsVectorFileWriter.NoError):
                self.log_msg(f"ATTENZIONE: salvataggio layer nodi/outfall manuali non riuscito. Codice: {err_code}")
                return None

            # Remove any previous copies of the same layer from the legend, then load the updated one.
            try:
                for lyr in list(QgsProject.instance().mapLayers().values()):
                    try:
                        if lyr.name() == "nodi_outfall_manuali_swmm":
                            QgsProject.instance().removeMapLayer(lyr.id())
                    except Exception:
                        pass
            except Exception:
                pass

            out_layer = QgsVectorLayer(gpkg_path, "nodi_outfall_manuali_swmm", "ogr")
            if out_layer.isValid():
                QgsProject.instance().addMapLayer(out_layer)
                self.log_msg(f"Layer nodi/outfall manuali salvato e caricato in mappa: {gpkg_path}")
                return out_layer
            else:
                self.log_msg(f"Layer nodi/outfall manuali salvato ma non caricato: {gpkg_path}")
                return None
        except Exception as e:
            self.log_msg(f"ATTENZIONE: errore nel salvataggio del layer nodi/outfall manuali: {e}")
            return None


    def _write_layer_to_gpkg(self, layer, gpkg_path, layer_name, overwrite_file=False):
        """Write a layer to a GeoPackage while preserving compatibility across QGIS versions."""
        try:
            if hasattr(QgsVectorFileWriter, "SaveVectorOptions") and hasattr(QgsVectorFileWriter, "writeAsVectorFormatV2"):
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName = "GPKG"
                opts.layerName = layer_name
                if overwrite_file:
                    opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
                else:
                    opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
                res = QgsVectorFileWriter.writeAsVectorFormatV2(layer, gpkg_path, QgsProject.instance().transformContext(), opts)
                err_code = res[0] if isinstance(res, tuple) else res
                return err_code in (0, QgsVectorFileWriter.NoError)
        except Exception:
            pass

        # Fallback for older QGIS versions: save the layer using the memory layer name.
        try:
            if overwrite_file and os.path.exists(gpkg_path):
                os.remove(gpkg_path)
            err = QgsVectorFileWriter.writeAsVectorFormat(layer, gpkg_path, "UTF-8", layer.crs(), "GPKG")
            err_code = err[0] if isinstance(err, tuple) else err
            return err_code in (0, QgsVectorFileWriter.NoError)
        except Exception:
            return False

    def _line_geometry_from_vertices(self, vertices):
        pts = []
        for v in vertices or []:
            try:
                if isinstance(v, QgsPointXY):
                    pts.append(QgsPointXY(v))
                elif hasattr(v, "x") and hasattr(v, "y"):
                    pts.append(QgsPointXY(float(v.x()), float(v.y())))
                elif isinstance(v, (list, tuple)) and len(v) >= 2:
                    pts.append(QgsPointXY(float(v[0]), float(v[1])))
            except Exception:
                pass
        if len(pts) < 2:
            return None
        return QgsGeometry.fromPolylineXY(pts)

    def _link_geometry_for_export(self, item, nodi):
        """Return the geometry of the added or converted link for the summary GeoPackage."""
        geom = self._line_geometry_from_vertices(item.get("vertices") or [])
        if geom is not None:
            return geom
        conduit = item.get("conduit") if isinstance(item.get("conduit"), dict) else None
        if conduit:
            geom = self._line_geometry_from_vertices(conduit.get("vertices") or [])
            if geom is not None:
                return geom
        fn = str(item.get("from_node", "") or "")
        tn = str(item.get("to_node", "") or "")
        if fn in nodi and tn in nodi:
            try:
                return QgsGeometry.fromPolylineXY([
                    QgsPointXY(float(nodi[fn]["x"]), float(nodi[fn]["y"])),
                    QgsPointXY(float(nodi[tn]["x"]), float(nodi[tn]["y"])),
                ])
            except Exception:
                return None
        return None

    def export_added_elements_to_gpkg(self, outdir, crs, nodi, storage_def=None, orifice_defs=None, weir_defs=None, pump_defs=None, manual_conduits=None):
        """Create a summary GeoPackage with all elements added or converted downstream of the model generation step."""
        try:
            if not outdir:
                return None
            crs_auth = crs.authid() if crs and crs.isValid() else "EPSG:4326"
            point_layer = QgsVectorLayer(f"Point?crs={crs_auth}", "swmm_elementi_aggiunti_punti", "memory")
            line_layer = QgsVectorLayer(f"LineString?crs={crs_auth}", "swmm_elementi_aggiunti_links", "memory")
            ppr = point_layer.dataProvider()
            lpr = line_layer.dataProvider()
            common_point_fields = [
                QgsField("tipo", QVariant.String),
                QgsField("element_id", QVariant.String),
                QgsField("source", QVariant.String),
                QgsField("quota_terr", QVariant.Double),
                QgsField("q_scorr", QVariant.Double),
                QgsField("max_depth", QVariant.Double),
                QgsField("fixed_stage", QVariant.Double),
                QgsField("gated", QVariant.String),
                QgsField("curve", QVariant.String),
                QgsField("params", QVariant.String),
            ]
            common_line_fields = [
                QgsField("tipo", QVariant.String),
                QgsField("link_id", QVariant.String),
                QgsField("from_node", QVariant.String),
                QgsField("to_node", QVariant.String),
                QgsField("source", QVariant.String),
                QgsField("replace_id", QVariant.String),
                QgsField("shape", QVariant.String),
                QgsField("geom1", QVariant.Double),
                QgsField("geom2", QVariant.Double),
                QgsField("length_m", QVariant.Double),
                QgsField("offset", QVariant.Double),
                QgsField("coeff", QVariant.Double),
                QgsField("curve", QVariant.String),
                QgsField("params", QVariant.String),
            ]
            ppr.addAttributes(common_point_fields)
            lpr.addAttributes(common_line_fields)
            point_layer.updateFields()
            line_layer.updateFields()

            def _json_params(d):
                try:
                    slim = {}
                    for k, v in (d or {}).items():
                        if k in ("conduit", "vertices", "rules", "curve_points"):
                            continue
                        if isinstance(v, (str, int, float, bool)) or v is None:
                            slim[k] = v
                    return json.dumps(slim, ensure_ascii=False)[:254]
                except Exception:
                    return ""

            def add_point(tipo, element_id, x, y, source="manual", qterr=None, qscorr=None, depth=None, stage=None, gated="", curve="", params=""):
                try:
                    f = QgsFeature(point_layer.fields())
                    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
                    f.setAttributes([
                        str(tipo), str(element_id), str(source or ""),
                        float(qterr) if qterr is not None else None,
                        float(qscorr) if qscorr is not None else None,
                        float(depth) if depth is not None else None,
                        float(stage) if stage is not None else None,
                        str(gated or ""), str(curve or ""), str(params or "")[:254],
                    ])
                    ppr.addFeature(f)
                except Exception:
                    pass

            def add_line(tipo, item, link_id=None, source=None, shape="", geom1=None, geom2=None, length=None, offset=None, coeff=None, curve=""):
                try:
                    link_id = str(link_id or item.get("cond_id") or item.get("link_id") or item.get("pump_id") or "")
                    if not link_id:
                        return
                    geom = self._link_geometry_for_export(item, nodi)
                    if geom is None or geom.isEmpty():
                        return
                    f = QgsFeature(line_layer.fields())
                    f.setGeometry(geom)
                    f.setAttributes([
                        str(tipo),
                        link_id,
                        str(item.get("from_node", "") or ""),
                        str(item.get("to_node", "") or ""),
                        str(source if source is not None else item.get("source", "") or ""),
                        str(item.get("replace_link", "") or ""),
                        str(shape or item.get("shape", "") or ""),
                        float(geom1 if geom1 is not None else item.get("geom1", item.get("height", 0.0)) or 0.0),
                        float(geom2 if geom2 is not None else item.get("geom2", item.get("width", 0.0)) or 0.0),
                        float(length if length is not None else item.get("length", geom.length()) or 0.0),
                        float(offset if offset is not None else item.get("offset", item.get("in_offset", 0.0)) or 0.0),
                        float(coeff if coeff is not None else item.get("coeff", 0.0) or 0.0),
                        str(curve or item.get("curve", "") or ""),
                        _json_params(item),
                    ])
                    lpr.addFeature(f)
                except Exception:
                    pass

            # Points added from the UI panels.
            table = getattr(self, "tbl_extra_nodes", None)
            if table is not None:
                for r in range(table.rowCount()):
                    node_id = self._table_text(table, r, 0)
                    if not node_id:
                        continue
                    qterr = _to_float(self._table_text(table, r, 1, "0").replace(",", "."), None)
                    qfond = _to_float(self._table_text(table, r, 2, "0").replace(",", "."), None)
                    depth = _to_float(self._table_text(table, r, 3, "1").replace(",", "."), None)
                    x = _to_float(self._table_text(table, r, 4, "").replace(",", "."), None)
                    y = _to_float(self._table_text(table, r, 5, "").replace(",", "."), None)
                    if None not in (x, y):
                        add_point("JUNCTION", node_id, x, y, "manual_panel", qterr, qfond, depth)

            table = getattr(self, "tbl_extra_outfalls", None)
            if table is not None:
                for r in range(table.rowCount()):
                    node_id = self._table_text(table, r, 0)
                    if not node_id:
                        continue
                    invert = _to_float(self._table_text(table, r, 1, "0").replace(",", "."), None)
                    stage_raw = self._table_text(table, r, 2, "").replace(",", ".")
                    stage = _to_float(stage_raw, None) if stage_raw else None
                    gated = (self._table_text(table, r, 3, "NO") or "NO").upper()
                    x = _to_float(self._table_text(table, r, 4, "").replace(",", "."), None)
                    y = _to_float(self._table_text(table, r, 5, "").replace(",", "."), None)
                    if None not in (x, y):
                        add_point("OUTFALL", node_id, x, y, "manual_panel", invert, invert, 0.0, stage, gated)

            # Storage unit: existing node converted.
            if storage_def:
                sid = str(storage_def.get("node_id", ""))
                if sid in nodi:
                    n = nodi[sid]
                    add_point("STORAGE", sid, n.get("x"), n.get("y"), "transformed_node", n.get("quota_terr"), n.get("quota_fondo"), n.get("max_depth"), None, "", storage_def.get("curve_name", ""), _json_params(storage_def))

            # Link aggiunti o trasformati.
            for c in (manual_conduits or []):
                if c.get("manual"):
                    add_line("CONDUIT", c, link_id=c.get("cond_id"), source="manual_panel")
            for o in (orifice_defs or []):
                add_line("ORIFICE", o, link_id=o.get("link_id"), source=o.get("source", ""), shape=o.get("shape", ""), geom1=o.get("height"), geom2=o.get("width"), offset=o.get("offset"), coeff=o.get("coeff"))
            for w in (weir_defs or []):
                add_line("WEIR", w, link_id=w.get("link_id"), source=w.get("source", ""), shape=w.get("shape", ""), geom1=w.get("height"), geom2=w.get("width"), offset=w.get("offset"), coeff=w.get("coeff"))
            for pmp in (pump_defs or []):
                add_line("PUMP", pmp, link_id=pmp.get("pump_id"), source=pmp.get("source", ""), curve=pmp.get("curve"))

            point_layer.updateExtents()
            line_layer.updateExtents()
            if point_layer.featureCount() == 0 and line_layer.featureCount() == 0:
                return None

            gpkg_path = os.path.join(outdir, "elementi_aggiunti_swmm.gpkg")
            try:
                if os.path.exists(gpkg_path):
                    os.remove(gpkg_path)
            except Exception:
                pass

            ok_any = False
            if point_layer.featureCount() > 0:
                ok_any = self._write_layer_to_gpkg(point_layer, gpkg_path, "swmm_elementi_aggiunti_punti", overwrite_file=True) or ok_any
            if line_layer.featureCount() > 0:
                ok_any = self._write_layer_to_gpkg(line_layer, gpkg_path, "swmm_elementi_aggiunti_links", overwrite_file=not os.path.exists(gpkg_path)) or ok_any
            if not ok_any:
                self.log_msg('WARNING: failed to save the added elements GeoPackage.')
                return None

            # Reload the summary layers, if available.
            try:
                for lyr in list(QgsProject.instance().mapLayers().values()):
                    if lyr.name() in ("swmm_elementi_aggiunti_punti", "swmm_elementi_aggiunti_links"):
                        QgsProject.instance().removeMapLayer(lyr.id())
            except Exception:
                pass
            for layer_name in ("swmm_elementi_aggiunti_punti", "swmm_elementi_aggiunti_links"):
                try:
                    lyr = QgsVectorLayer(f"{gpkg_path}|layername={layer_name}", layer_name, "ogr")
                    if lyr.isValid():
                        QgsProject.instance().addMapLayer(lyr)
                except Exception:
                    pass
            self.log_msg(f"GeoPackage elementi aggiunti salvato: {gpkg_path}")
            return gpkg_path
        except Exception as e:
            self.log_msg(f"ATTENZIONE: errore nel salvataggio del GeoPackage elementi aggiunti: {e}")
            return None

    def get_manual_conduit_definitions(self, nodi):
        table = getattr(self, "tbl_extra_links", None)
        if table is None or table.rowCount() == 0:
            return []
        node_ids = set(nodi.keys())
        conduits = []
        seen = set()
        for r in range(table.rowCount()):
            link_id = self._table_text(table, r, 0)
            if not link_id:
                continue
            if link_id in seen:
                raise Exception(f"Condotta manuale duplicata: {link_id}.")
            seen.add(link_id)
            from_node = self._table_text(table, r, 1)
            to_node = self._table_text(table, r, 2)
            if from_node not in node_ids or to_node not in node_ids:
                raise Exception(f"Condotta manuale '{link_id}': nodo monte/valle non presente nel modello.")
            shape = self._table_text(table, r, 3, "CIRCULAR").upper()
            geom1 = _to_float(self._table_text(table, r, 4, "0.300").replace(",", "."), None)
            geom2 = _to_float(self._table_text(table, r, 5, "0").replace(",", "."), None)
            rough = _to_float(self._table_text(table, r, 6, f"{self.spn_roughness.value():.5f}").replace(",", "."), None)
            inoff = _to_float(self._table_text(table, r, 7, "0").replace(",", "."), None)
            outoff = _to_float(self._table_text(table, r, 8, "0").replace(",", "."), None)
            length = _to_float(self._table_text(table, r, 9, "1").replace(",", "."), None)
            try:
                shape, geom1, geom2 = self._normalize_xsection_values(shape, geom1, geom2)
            except Exception as e:
                raise Exception(f"Condotta manuale '{link_id}': {e}")
            if None in (rough, inoff, outoff, length) or rough <= 0 or length <= 0:
                raise Exception(f"Condotta manuale '{link_id}': roughness e lunghezza devono essere numerici e positivi.")
            verts = []
            if r < len(self.manual_link_definitions):
                verts = list(self.manual_link_definitions[r].get("vertices") or [])
            if not verts:
                verts = [(nodi[from_node]["x"], nodi[from_node]["y"]), (nodi[to_node]["x"], nodi[to_node]["y"])]
            conduits.append({
                "cond_id": str(link_id),
                "from_node": str(from_node),
                "to_node": str(to_node),
                "length": max(float(length), 0.1),
                "diam_m": max(float(geom1), 0.05),
                "shape": shape,
                "geom1": float(geom1),
                "geom2": float(geom2),
                "geom3": 0.0,
                "geom4": 0.0,
                "roughness": float(rough),
                "z_monte": None,
                "z_valle": None,
                "in_offset": max(float(inoff), 0.0),
                "out_offset": max(float(outoff), 0.0),
                "slope": None,
                "vertices": verts,
                "manual": True,
            })
        return conduits


    def _normalize_xsection_values(self, shape, geom1, geom2=0.0, geom3=0.0, geom4=0.0):
        shape = (shape or "CIRCULAR").strip().upper()
        aliases = {
            "RECT": "RECT_CLOSED",
            "RECTANGULAR": "RECT_CLOSED",
            "RETTANGOLARE": "RECT_CLOSED",
            "OVOIDALE": "EGG",
            "OVOID": "EGG",
            "ARCO": "ARCH",
        }
        shape = aliases.get(shape, shape)
        allowed = {"CIRCULAR", "RECT_CLOSED", "EGG", "ARCH"}
        if shape not in allowed:
            raise Exception(f"Shape '{shape}' non supportata. Usa CIRCULAR, RECT_CLOSED, EGG oppure ARCH.")
        geom1 = _to_float(str(geom1).replace(",", "."), None)
        geom2 = _to_float(str(geom2).replace(",", "."), 0.0)
        geom3 = _to_float(str(geom3).replace(",", "."), 0.0)
        geom4 = _to_float(str(geom4).replace(",", "."), 0.0)
        if geom1 is None or geom1 <= 0:
            raise Exception("Geom1 must be numeric and greater than zero.")
        if shape == "RECT_CLOSED":
            if geom2 is None or geom2 <= 0:
                raise Exception("For RECT_CLOSED, Geom2/width must also be greater than zero.")
        else:
            geom2 = 0.0
        return shape, float(geom1), float(geom2 or 0.0)

    def add_conduit_shape_override_row(self, cond_id="", shape=None, geom1=None, geom2=None):
        table = getattr(self, "tbl_conduit_shape_overrides", None)
        if table is None:
            return
        row = table.rowCount()
        table.insertRow(row)
        if shape is None or geom1 is None:
            shape, geom1, geom2 = self._current_extra_link_shape_values()
        table.setItem(row, 0, QTableWidgetItem(str(cond_id or "")))
        cmb = QComboBox()
        cmb.addItems(["CIRCULAR", "RECT_CLOSED", "EGG", "ARCH"])
        idx = cmb.findText(str(shape).upper())
        if idx >= 0:
            cmb.setCurrentIndex(idx)
        table.setCellWidget(row, 1, cmb)
        table.setItem(row, 2, QTableWidgetItem(f"{float(geom1):.3f}"))
        table.setItem(row, 3, QTableWidgetItem(f"{float(geom2 or 0.0):.3f}"))

    def _conduit_id_from_feature(self, f):
        try:
            cid = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
            return str(cid) if cid not in (None, "") else f"COND_{f.id()}"
        except Exception:
            return f"COND_{f.id()}"

    def add_selected_conduit_shape_overrides(self):
        try:
            cond_layer = self.get_layer(self.cmb_condotte) if hasattr(self, "cmb_condotte") else None
            if cond_layer is None:
                raise Exception('Select the SWMM input conduit layer.')
            selected = list(cond_layer.selectedFeatures())
            if not selected:
                raise Exception('Select one or more conduits in the QGIS conduit layer.')
            shape, geom1, geom2 = self._current_extra_link_shape_values()
            existing = set()
            table = getattr(self, "tbl_conduit_shape_overrides", None)
            for r in range(table.rowCount()):
                existing.add(self._table_text(table, r, 0))
            added = 0
            for f in selected:
                cid = self._conduit_id_from_feature(f)
                if cid in existing:
                    continue
                self.add_conduit_shape_override_row(cid, shape, geom1, geom2)
                added += 1
            self.log_msg(f"Conduit shape edits added from selection: {added}.")
        except Exception as e:
            QMessageBox.warning(self, 'Conduit shape edit', str(e))

    def get_conduit_shape_overrides(self, conduits):
        table = getattr(self, "tbl_conduit_shape_overrides", None)
        if table is None or table.rowCount() == 0:
            return {}
        conduit_ids = {str(c.get("cond_id")) for c in conduits}
        overrides = {}
        for r in range(table.rowCount()):
            cid = self._table_text(table, r, 0)
            if not cid:
                continue
            if cid not in conduit_ids:
                raise Exception(f"Modifica forma condotta riga {r+1}: la condotta '{cid}' non è presente nel modello.")
            if cid in overrides:
                raise Exception(f"Modifica forma condotta duplicata per '{cid}'.")
            shape = self._table_text(table, r, 1, "CIRCULAR").upper()
            geom1 = _to_float(self._table_text(table, r, 2, "0.300").replace(",", "."), None)
            geom2 = _to_float(self._table_text(table, r, 3, "0").replace(",", "."), None)
            try:
                shape, geom1, geom2 = self._normalize_xsection_values(shape, geom1, geom2)
            except Exception as e:
                raise Exception(f"Modifica forma condotta '{cid}': {e}")
            overrides[cid] = {"shape": shape, "geom1": geom1, "geom2": geom2, "geom3": 0.0, "geom4": 0.0}
        return overrides

    def apply_conduit_shape_overrides(self, conduits):
        overrides = self.get_conduit_shape_overrides(conduits)
        if not overrides:
            return 0
        for c in conduits:
            cid = str(c.get("cond_id"))
            if cid in overrides:
                c.update(overrides[cid])
                c["diam_m"] = float(overrides[cid]["geom1"])
        return len(overrides)

    def add_storage_curve_row(self, depth=None, area=None):
        table = getattr(self, "tbl_storage_curve", None)
        if table is None:
            return
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem("" if depth is None else f"{float(depth):.3f}"))
        table.setItem(row, 1, QTableWidgetItem("" if area is None else f"{float(area):.3f}"))

    def remove_storage_curve_row(self):
        table = getattr(self, "tbl_storage_curve", None)
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            table.removeRow(row)

    def _read_storage_curve_points(self, require_complete=True):
        curve = []
        table = getattr(self, "tbl_storage_curve", None)
        if table is None:
            return curve
        for row in range(table.rowCount()):
            depth_item = table.item(row, 0)
            area_item = table.item(row, 1)
            depth_raw = depth_item.text().strip().replace(",", ".") if depth_item else ""
            area_raw = area_item.text().strip().replace(",", ".") if area_item else ""
            if not depth_raw and not area_raw:
                continue
            depth = _to_float(depth_raw, None)
            area = _to_float(area_raw, None)
            if depth is None or area is None:
                if require_complete:
                    raise Exception(f"Curva storage non valida alla riga {row + 1}: inserisci Depth e Area numerici.")
                continue
            if depth < 0:
                raise Exception(f"Curva storage non valida alla riga {row + 1}: Depth non può essere negativo.")
            if area < 0:
                raise Exception(f"Curva storage non valida alla riga {row + 1}: Area non può essere negativa.")
            curve.append((float(depth), float(area)))
        curve.sort(key=lambda p: p[0])
        return curve

    def show_storage_curve_preview(self):
        try:
            curve = self._read_storage_curve_points(require_complete=True)
            if len(curve) < 2:
                QMessageBox.warning(self, 'Storage chart', 'Enter at least two Depth/Area points to preview the curve.')
                return
            # For display only, swap the axes: the table and INP file remain
            # in the correct SWMM Depth/Area format, while the chart displays
            # Area on the x-axis and Depth on the y-axis for a more natural
            # storage section/volume interpretation.
            plot_curve = [(area, depth) for depth, area in curve]
            dlg = CurvePreviewDialog(self, "Storage curve - Area / Depth", "Area [m²]", "Depth [m]", plot_curve)
            dlg.exec_()
        except Exception as e:
            QMessageBox.warning(self, 'Storage chart', str(e))

    def get_storage_definition(self, nodi, outfall_node):
        storage_node = ""
        if hasattr(self, "txt_storage_node"):
            storage_node = self.txt_storage_node.text().strip()
        if not storage_node:
            return None
        outfall_set = set(outfall_node) if isinstance(outfall_node, (set, list, tuple)) else {str(outfall_node)}
        if storage_node in outfall_set:
            raise Exception('The storage node cannot be the same as an outfall node.')
        if storage_node not in nodi:
            raise Exception(
                f"Il nodo storage '{storage_node}' non è presente tra i nodi interni letti dal layer nodi. "
                "Check the node ID or the catchment boundary."
            )

        curve = self._read_storage_curve_points(require_complete=True)

        if len(curve) < 2:
            raise Exception('The storage curve must contain at least two Depth/Area points.')
        curve.sort(key=lambda p: p[0])
        for i in range(1, len(curve)):
            if curve[i][0] <= curve[i - 1][0]:
                raise Exception('The storage curve must have increasing, non-duplicated Depth values.')
        if max(area for _depth, area in curve) <= 0:
            raise Exception('The storage curve must contain at least one Area value greater than zero.')

        return {
            "node_id": storage_node,
            "curve_name": f"STOR_{re.sub(r'[^A-Za-z0-9_]', '_', storage_node)}",
            "curve": curve,
        }

    def get_orifice_definition(self, conduits):
        orifice_id = ""
        if hasattr(self, "txt_orifice_link"):
            orifice_id = self.txt_orifice_link.text().strip()

        rules_text = ""
        if hasattr(self, "txt_control_rules"):
            rules_text = self.txt_control_rules.toPlainText().strip()

        if not orifice_id:
            if rules_text:
                raise Exception('You entered control rules, but did not specify the conduit to convert into an orifice.')
            return None

        conduit_by_id = {str(c.get("cond_id")): c for c in conduits}
        if orifice_id not in conduit_by_id:
            raise Exception(
                f"La condotta/orifizio '{orifice_id}' non è presente tra le condotte lette dal modello. "
                "Check the conduit ID."
            )

        shape = self.cmb_orifice_shape.currentText().strip() if hasattr(self, "cmb_orifice_shape") else "CIRCULAR"
        orifice_type = self.cmb_orifice_type.currentText().strip() if hasattr(self, "cmb_orifice_type") else "SIDE"
        height = float(self.spn_orifice_height.value()) if hasattr(self, "spn_orifice_height") else 0.3
        width = float(self.spn_orifice_width.value()) if hasattr(self, "spn_orifice_width") else height
        coeff = float(self.spn_orifice_coeff.value()) if hasattr(self, "spn_orifice_coeff") else 0.65

        if shape == "CIRCULAR":
            width = 0.0
        if height <= 0 or (shape != "CIRCULAR" and width <= 0):
            raise Exception('Orifice dimensions must be greater than zero.')
        if coeff <= 0:
            raise Exception('The orifice discharge coefficient must be greater than zero.')

        rules_lines = []
        if rules_text:
            rules_lines = [line.rstrip() for line in rules_text.splitlines()]
            if not any(line.strip().upper().startswith("RULE ") for line in rules_lines):
                raise Exception("Control rules must contain at least one 'RULE rule_name' line.")

        return {
            "link_id": orifice_id,
            "conduit": conduit_by_id[orifice_id],
            "type": orifice_type,
            "shape": shape,
            "height": height,
            "width": width,
            "coeff": coeff,
            "rules": rules_lines,
        }


    def _line_vertices_from_feature(self, f):
        try:
            geom = f.geometry()
            line = self.first_polyline(geom) if geom and not geom.isEmpty() else []
            return [(QgsPointXY(p).x(), QgsPointXY(p).y()) for p in line] if line else []
        except Exception:
            return []

    def _current_orifice_template(self):
        shape = self.cmb_orifice_shape.currentText().strip() if hasattr(self, "cmb_orifice_shape") else "CIRCULAR"
        orifice_type = self.cmb_orifice_type.currentText().strip() if hasattr(self, "cmb_orifice_type") else "SIDE"
        height = float(self.spn_orifice_height.value()) if hasattr(self, "spn_orifice_height") else 0.3
        width = float(self.spn_orifice_width.value()) if hasattr(self, "spn_orifice_width") else height
        coeff = float(self.spn_orifice_coeff.value()) if hasattr(self, "spn_orifice_coeff") else 0.65
        offset = float(self.spn_orifice_offset.value()) if hasattr(self, "spn_orifice_offset") else 0.0
        if shape == "CIRCULAR":
            width = 0.0
        if height <= 0 or (shape != "CIRCULAR" and width <= 0):
            raise Exception('Orifice dimensions must be greater than zero.')
        if coeff <= 0:
            raise Exception('The orifice discharge coefficient must be greater than zero.')
        if offset < 0:
            raise Exception('The orifice inlet offset cannot be negative.')
        return {"type": orifice_type, "shape": shape, "height": height, "width": width, "coeff": coeff, "offset": offset}

    def _refresh_orifice_table(self):
        table = getattr(self, "tbl_orifices", None)
        if table is None:
            return
        table.setRowCount(len(self.orifice_definitions))
        cols = ["link_id", "from_node", "to_node", "type", "shape", "offset", "height", "width", "source"]
        for r, item in enumerate(self.orifice_definitions):
            for c, key in enumerate(cols):
                v = item.get(key, "")
                if key in ("offset", "height", "width"):
                    v = f"{float(v):.3f}"
                table.setItem(r, c, QTableWidgetItem(str(v)))

    def _add_orifice_definition(self, item):
        link_id = str(item["link_id"])
        if any(str(o.get("link_id")) == link_id for o in self.orifice_definitions):
            raise Exception(f"Esiste già un orifizio con ID '{link_id}'.")
        if any(str(p.get("pump_id")) == link_id for p in self.pump_definitions):
            raise Exception(f"Esiste già una pompa con ID '{link_id}'.")
        if any(str(w.get("link_id")) == link_id for w in self.weir_definitions):
            raise Exception(f"Esiste già un weir con ID '{link_id}'.")
        self.orifice_definitions.append(item)
        self._refresh_orifice_table()
        self.log_msg(f"Orifice added: {link_id} ({item['from_node']} -> {item['to_node']}).")

    def add_orifice_from_conduit(self):
        try:
            link_id = self.txt_orifice_link.text().strip() if hasattr(self, "txt_orifice_link") else ""
            if not link_id:
                raise Exception('Enter the ID of the conduit to convert into an orifice.')
            cond_layer = self.get_layer(self.cmb_condotte)
            if cond_layer is None:
                raise Exception('Select the SWMM input conduit layer.')
            template = self._current_orifice_template()
            for f in cond_layer.getFeatures():
                cond_id = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
                if cond_id is None:
                    cond_id = f"COND_{f.id()}"
                if str(cond_id) != link_id:
                    continue
                from_node = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
                to_node = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])
                if from_node is None or to_node is None:
                    raise Exception('The selected conduit does not contain recognizable upstream/downstream node fields.')
                d = dict(template)
                d.update({"link_id": link_id, "from_node": str(from_node), "to_node": str(to_node), "source": "conduit", "replace_link": link_id, "vertices": self._line_vertices_from_feature(f)})
                self._add_orifice_definition(d)
                return
            raise Exception(f"Condotta '{link_id}' non trovata nel layer condotte.")
        except Exception as e:
            QMessageBox.critical(self, 'Orifice', str(e))

    def start_draw_orifice_link(self):
        try:
            node_layer = self.get_layer(self.cmb_nodi)
            if node_layer is None:
                raise Exception('Select the SWMM input node layer before drawing the orifice.')
            if getattr(self, "regulator_draw_tool", None):
                try:
                    self.regulator_draw_tool.clear_drawing()
                except Exception:
                    pass
            snap_tol = float(self.spn_snap_tolerance.value()) if hasattr(self, "spn_snap_tolerance") else 2.0
            self.regulator_draw_tool = PumpLinkDrawTool(self.canvas, node_layer, self.on_orifice_link_drawn, self.log_msg, snap_tol, 'orifice', self._extra_node_snap_items)
            self.canvas.setMapTool(self.regulator_draw_tool)
            self.log_msg('Orifice tool active: click the upstream node, any intermediate vertices, and finally the downstream node.')
        except Exception as e:
            QMessageBox.critical(self, 'Orifice', str(e))

    def on_orifice_link_drawn(self, nodes, vertices_layer=None):
        try:
            if len(nodes) != 2:
                return
            vertices_layer = vertices_layer or [nodes[0]["point_layer"], nodes[1]["point_layer"]]
            template = self._current_orifice_template()
            idx = len(self.orifice_definitions) + 1
            existing = {str(o.get("link_id")) for o in self.orifice_definitions}
            link_id = f"ORIFICE_{idx}"
            while link_id in existing:
                idx += 1
                link_id = f"ORIFICE_{idx}"
            d = dict(template)
            d.update({"link_id": link_id, "from_node": nodes[0]["node_id"], "to_node": nodes[1]["node_id"], "source": "drawn", "replace_link": "", "vertices": [(pt.x(), pt.y()) for pt in vertices_layer]})
            self._add_orifice_definition(d)
            try:
                self.canvas.unsetMapTool(self.regulator_draw_tool)
            except Exception:
                pass
            self.regulator_draw_tool = None
        except Exception as e:
            QMessageBox.critical(self, 'Orifice', str(e))

    def remove_selected_orifice(self):
        table = getattr(self, "tbl_orifices", None)
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            if 0 <= row < len(self.orifice_definitions):
                self.orifice_definitions.pop(row)
        self._refresh_orifice_table()

    def _current_weir_template(self):
        weir_type = self.cmb_weir_type.currentText().strip() if hasattr(self, "cmb_weir_type") else "TRANSVERSE"
        shape = self.cmb_weir_shape.currentText().strip() if hasattr(self, "cmb_weir_shape") else "RECT_OPEN"
        height = float(self.spn_weir_height.value()) if hasattr(self, "spn_weir_height") else 0.3
        width = float(self.spn_weir_width.value()) if hasattr(self, "spn_weir_width") else 1.0
        coeff = float(self.spn_weir_coeff.value()) if hasattr(self, "spn_weir_coeff") else 1.7
        endcon = float(self.spn_weir_endcon.value()) if hasattr(self, "spn_weir_endcon") else 0.0
        offset = float(self.spn_weir_offset.value()) if hasattr(self, "spn_weir_offset") else 0.0
        if height < 0 or width <= 0 or coeff <= 0:
            raise Exception("The weir dimensions and discharge coefficient must be valid.")
        if offset < 0:
            raise Exception('The weir inlet offset / crest height cannot be negative.')
        return {"type": weir_type, "shape": shape, "height": height, "width": width, "coeff": coeff, "endcon": endcon, "offset": offset}

    def _refresh_weir_table(self):
        table = getattr(self, "tbl_weirs", None)
        if table is None:
            return
        table.setRowCount(len(self.weir_definitions))
        cols = ["link_id", "from_node", "to_node", "type", "shape", "offset", "height", "width", "source"]
        for r, item in enumerate(self.weir_definitions):
            for c, key in enumerate(cols):
                v = item.get(key, "")
                if key in ("offset", "height", "width"):
                    v = f"{float(v):.3f}"
                table.setItem(r, c, QTableWidgetItem(str(v)))

    def _add_weir_definition(self, item):
        link_id = str(item["link_id"])
        if any(str(w.get("link_id")) == link_id for w in self.weir_definitions):
            raise Exception(f"Esiste già un weir con ID '{link_id}'.")
        if any(str(o.get("link_id")) == link_id for o in self.orifice_definitions):
            raise Exception(f"Esiste già un orifizio con ID '{link_id}'.")
        if any(str(p.get("pump_id")) == link_id for p in self.pump_definitions):
            raise Exception(f"Esiste già una pompa con ID '{link_id}'.")
        self.weir_definitions.append(item)
        self._refresh_weir_table()
        self.log_msg(f"Weir aggiunto: {link_id} ({item['from_node']} -> {item['to_node']}).")

    def add_weir_from_conduit(self):
        try:
            link_id = self.txt_weir_link.text().strip() if hasattr(self, "txt_weir_link") else ""
            if not link_id:
                raise Exception('Enter the ID of the conduit to convert into a weir.')
            cond_layer = self.get_layer(self.cmb_condotte)
            if cond_layer is None:
                raise Exception('Select the SWMM input conduit layer.')
            template = self._current_weir_template()
            for f in cond_layer.getFeatures():
                cond_id = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
                if cond_id is None:
                    cond_id = f"COND_{f.id()}"
                if str(cond_id) != link_id:
                    continue
                from_node = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
                to_node = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])
                if from_node is None or to_node is None:
                    raise Exception('The selected conduit does not contain recognizable upstream/downstream node fields.')
                d = dict(template)
                d.update({"link_id": link_id, "from_node": str(from_node), "to_node": str(to_node), "source": "conduit", "replace_link": link_id, "vertices": self._line_vertices_from_feature(f)})
                self._add_weir_definition(d)
                return
            raise Exception(f"Condotta '{link_id}' non trovata nel layer condotte.")
        except Exception as e:
            QMessageBox.critical(self, "Weir", str(e))

    def start_draw_weir_link(self):
        try:
            node_layer = self.get_layer(self.cmb_nodi)
            if node_layer is None:
                raise Exception('Select the SWMM input node layer before drawing the weir.')
            if getattr(self, "regulator_draw_tool", None):
                try:
                    self.regulator_draw_tool.clear_drawing()
                except Exception:
                    pass
            snap_tol = float(self.spn_snap_tolerance.value()) if hasattr(self, "spn_snap_tolerance") else 2.0
            self.regulator_draw_tool = PumpLinkDrawTool(self.canvas, node_layer, self.on_weir_link_drawn, self.log_msg, snap_tol, "weir", self._extra_node_snap_items)
            self.canvas.setMapTool(self.regulator_draw_tool)
            self.log_msg('Weir tool active: click the upstream node, any intermediate vertices, and finally the downstream node.')
        except Exception as e:
            QMessageBox.critical(self, "Weir", str(e))

    def on_weir_link_drawn(self, nodes, vertices_layer=None):
        try:
            if len(nodes) != 2:
                return
            vertices_layer = vertices_layer or [nodes[0]["point_layer"], nodes[1]["point_layer"]]
            template = self._current_weir_template()
            idx = len(self.weir_definitions) + 1
            existing = {str(w.get("link_id")) for w in self.weir_definitions}
            link_id = f"WEIR_{idx}"
            while link_id in existing:
                idx += 1
                link_id = f"WEIR_{idx}"
            d = dict(template)
            d.update({"link_id": link_id, "from_node": nodes[0]["node_id"], "to_node": nodes[1]["node_id"], "source": "drawn", "replace_link": "", "vertices": [(pt.x(), pt.y()) for pt in vertices_layer]})
            self._add_weir_definition(d)
            try:
                self.canvas.unsetMapTool(self.regulator_draw_tool)
            except Exception:
                pass
            self.regulator_draw_tool = None
        except Exception as e:
            QMessageBox.critical(self, "Weir", str(e))

    def remove_selected_weir(self):
        table = getattr(self, "tbl_weirs", None)
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            if 0 <= row < len(self.weir_definitions):
                self.weir_definitions.pop(row)
        self._refresh_weir_table()

    def _sync_orifice_table_into_definitions(self):
        """Synchronize edits made directly in the orifice table.

        This ensures that Inlet offset, as well as type, shape and dimensions,
        are not only valid when the "Add orifice" button is pressed. If the user
        edits the table row before generating the model, the current value is
        actually written to [ORIFICES].
        """
        table = getattr(self, "tbl_orifices", None)
        if table is None:
            return
        cols = ["link_id", "from_node", "to_node", "type", "shape", "offset", "height", "width", "source"]
        n = min(table.rowCount(), len(self.orifice_definitions))
        for r in range(n):
            item = self.orifice_definitions[r]
            for c, key in enumerate(cols):
                cell = table.item(r, c)
                if cell is None:
                    continue
                txt = cell.text().strip()
                if key in ("offset", "height", "width"):
                    if txt != "":
                        try:
                            item[key] = float(txt.replace(",", "."))
                        except Exception:
                            raise Exception(f"Valore non numerico nella tabella orifices, riga {r + 1}, colonna {key}.")
                elif key in ("type", "shape") and txt:
                    item[key] = txt.upper()
                elif key in ("link_id", "from_node", "to_node", "source") and txt:
                    item[key] = txt

    def _update_existing_orifice_from_current_fields(self):
        """Update an existing orifice when the user changes the fields above.

        Typical case: the user inserts conduit C_12 and then changes the Inlet
        offset without pressing "Add orifice" again. Before generation, the
        plugin must use the current value.
        """
        link_id = self.txt_orifice_link.text().strip() if hasattr(self, "txt_orifice_link") else ""
        if not link_id:
            return
        try:
            template = self._current_orifice_template()
        except Exception:
            return
        for item in self.orifice_definitions:
            if str(item.get("link_id")) == link_id or str(item.get("replace_link")) == link_id:
                item.update(template)

    def get_orifice_definitions(self, conduits, nodi):
        # Backward compatible: if the text field is filled and not already added, add it now.
        link_id = self.txt_orifice_link.text().strip() if hasattr(self, "txt_orifice_link") else ""
        if link_id and not any(str(o.get("link_id")) == link_id or str(o.get("replace_link")) == link_id for o in self.orifice_definitions):
            self.add_orifice_from_conduit()
        # Before writing the INP file, synchronize any user-edited values,
        # in particolare l'Inlet offset dell'orifice.
        self._sync_orifice_table_into_definitions()
        self._update_existing_orifice_from_current_fields()
        self._refresh_orifice_table()
        rules_text = self.txt_control_rules.toPlainText().strip() if hasattr(self, "txt_control_rules") else ""
        node_ids = set(nodi.keys())
        conduit_ids = {str(c.get("cond_id")) for c in conduits}
        validated = []
        for o in self.orifice_definitions:
            if o.get("replace_link") and str(o["replace_link"]) not in conduit_ids:
                raise Exception(f"La condotta da trasformare in orifizio '{o['replace_link']}' non è presente nel modello.")
            if str(o.get("from_node")) not in node_ids or str(o.get("to_node")) not in node_ids:
                raise Exception(f"L'orifizio '{o.get('link_id')}' collega nodi non presenti nel modello.")
            item = dict(o)
            item["rules"] = []
            validated.append(item)
        if rules_text:
            if not validated:
                raise Exception('You entered control rules, but did not add any orifice.')
            rules_lines = [line.rstrip() for line in rules_text.splitlines()]
            if not any(line.strip().upper().startswith("RULE ") for line in rules_lines):
                raise Exception("Orifice control rules must contain at least one 'RULE rule_name' line.")
            for item in validated:
                item["rules"] = rules_lines
        return validated

    def get_weir_definitions(self, conduits, nodi):
        link_id = self.txt_weir_link.text().strip() if hasattr(self, "txt_weir_link") else ""
        if link_id and not any(str(w.get("link_id")) == link_id for w in self.weir_definitions):
            self.add_weir_from_conduit()
        rules_text = self.txt_weir_control_rules.toPlainText().strip() if hasattr(self, "txt_weir_control_rules") else ""
        node_ids = set(nodi.keys())
        conduit_ids = {str(c.get("cond_id")) for c in conduits}
        validated = []
        for w in self.weir_definitions:
            if w.get("replace_link") and str(w["replace_link"]) not in conduit_ids:
                raise Exception(f"La condotta da trasformare in weir '{w['replace_link']}' non è presente nel modello.")
            if str(w.get("from_node")) not in node_ids or str(w.get("to_node")) not in node_ids:
                raise Exception(f"Il weir '{w.get('link_id')}' collega nodi non presenti nel modello.")
            item = dict(w)
            item["rules"] = []
            validated.append(item)
        if rules_text:
            if not validated:
                raise Exception('You entered weir control rules, but did not add any weir.')
            rules_lines = [line.rstrip() for line in rules_text.splitlines()]
            if not any(line.strip().upper().startswith("RULE ") for line in rules_lines):
                raise Exception("Weir control rules must contain at least one 'RULE rule_name' line.")
            for item in validated:
                item["rules"] = rules_lines
        return validated

    def _current_pump_template(self):
        curve_name = self.txt_pump_curve_name.text().strip() if hasattr(self, "txt_pump_curve_name") else "PUMP_CURVE"
        curve_name = curve_name or "PUMP_CURVE"
        curve_type = self.cmb_pump_curve_type.currentText().strip() if hasattr(self, "cmb_pump_curve_type") else "PUMP3"
        if curve_type not in ("PUMP1", "PUMP2", "PUMP3", "PUMP4"):
            curve_type = "PUMP3"
        startup = float(self.spn_pump_startup.value()) if hasattr(self, "spn_pump_startup") else 1.0
        shutoff = float(self.spn_pump_shutoff.value()) if hasattr(self, "spn_pump_shutoff") else 0.2
        if startup < shutoff:
            raise Exception("For pumps, the startup depth must be greater than or equal to the shutoff depth.")
        status = self.cmb_pump_status.currentText().strip() if hasattr(self, "cmb_pump_status") else "ON"
        return {"curve": curve_name, "curve_type": curve_type, "startup": startup, "shutoff": shutoff, "status": status}

    def _refresh_pump_table(self):
        table = getattr(self, "tbl_pumps", None)
        if table is None:
            return
        table.setRowCount(len(self.pump_definitions))
        cols = ["pump_id", "from_node", "to_node", "curve", "curve_type", "startup", "shutoff", "status", "source"]
        for r, p in enumerate(self.pump_definitions):
            for c, key in enumerate(cols):
                v = p.get(key, "")
                if key in ("startup", "shutoff"):
                    v = f"{float(v):.3f}"
                table.setItem(r, c, QTableWidgetItem(str(v)))

    def _add_pump_definition(self, pump_def):
        pump_id = str(pump_def["pump_id"])
        if any(str(p.get("pump_id")) == pump_id for p in self.pump_definitions):
            raise Exception(f"Esiste già una pompa con ID '{pump_id}'.")
        self.pump_definitions.append(pump_def)
        self._refresh_pump_table()
        self.log_msg(f"Pump added: {pump_id} ({pump_def['from_node']} -> {pump_def['to_node']}).")

    def add_pump_from_conduit(self):
        try:
            pump_link = self.txt_pump_link.text().strip() if hasattr(self, "txt_pump_link") else ""
            if not pump_link:
                raise Exception('Enter the ID of the conduit to convert into a pump.')
            cond_layer = self.get_layer(self.cmb_condotte)
            if cond_layer is None:
                raise Exception('Select the SWMM input conduit layer.')
            template = self._current_pump_template()
            for f in cond_layer.getFeatures():
                cond_id = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
                if cond_id is None:
                    cond_id = f"COND_{f.id()}"
                if str(cond_id) != pump_link:
                    continue
                from_node = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
                to_node = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])
                if from_node is None or to_node is None:
                    raise Exception('The selected conduit does not contain recognizable upstream/downstream node fields.')
                geom = f.geometry()
                line = self.first_polyline(geom) if geom and not geom.isEmpty() else []
                vertices = [(QgsPointXY(p).x(), QgsPointXY(p).y()) for p in line] if line else []
                self._add_pump_definition({
                    "pump_id": pump_link,
                    "from_node": str(from_node),
                    "to_node": str(to_node),
                    "curve": template["curve"],
                    "curve_type": template["curve_type"],
                    "startup": template["startup"],
                    "shutoff": template["shutoff"],
                    "status": template["status"],
                    "source": "conduit",
                    "replace_link": pump_link,
                    "vertices": vertices,
                })
                return
            raise Exception(f"Condotta '{pump_link}' non trovata nel layer condotte.")
        except Exception as e:
            QMessageBox.critical(self, 'Pump', str(e))

    def start_draw_pump_link(self):
        try:
            node_layer = self.get_layer(self.cmb_nodi)
            if node_layer is None:
                raise Exception('Select the SWMM input node layer before drawing the pump.')
            if self.pump_draw_tool:
                try:
                    self.pump_draw_tool.clear_drawing()
                except Exception:
                    pass
            snap_tol = float(self.spn_snap_tolerance.value()) if hasattr(self, "spn_snap_tolerance") else 2.0
            self.pump_draw_tool = PumpLinkDrawTool(
                self.canvas,
                node_layer,
                self.on_pump_link_drawn,
                self.log_msg,
                snap_tol,
                'pump',
                self._extra_node_snap_items,
            )
            self.canvas.setMapTool(self.pump_draw_tool)
            self.log_msg('Pump tool active: click the upstream node, any intermediate vertices, and finally the downstream node.')
        except Exception as e:
            QMessageBox.critical(self, 'Pump', str(e))

    def on_pump_link_drawn(self, nodes, vertices_layer=None):
        try:
            if len(nodes) != 2:
                return
            vertices_layer = vertices_layer or [nodes[0]["point_layer"], nodes[1]["point_layer"]]
            template = self._current_pump_template()
            idx = len(self.pump_definitions) + 1
            existing = {str(p.get("pump_id")) for p in self.pump_definitions}
            pump_id = f"PUMP_{idx}"
            while pump_id in existing:
                idx += 1
                pump_id = f"PUMP_{idx}"
            self._add_pump_definition({
                "pump_id": pump_id,
                "from_node": nodes[0]["node_id"],
                "to_node": nodes[1]["node_id"],
                "curve": template["curve"],
                "curve_type": template["curve_type"],
                "startup": template["startup"],
                "shutoff": template["shutoff"],
                "status": template["status"],
                "source": "drawn",
                "replace_link": "",
                "vertices": [(pt.x(), pt.y()) for pt in vertices_layer],
            })
            try:
                self.canvas.unsetMapTool(self.pump_draw_tool)
            except Exception:
                pass
            self.pump_draw_tool = None
        except Exception as e:
            QMessageBox.critical(self, 'Pump', str(e))

    def remove_selected_pump(self):
        table = getattr(self, "tbl_pumps", None)
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            if 0 <= row < len(self.pump_definitions):
                self.pump_definitions.pop(row)
        self._refresh_pump_table()

    def pump_curve_active_columns(self):
        curve_type = self.cmb_pump_curve_type.currentText().strip() if hasattr(self, "cmb_pump_curve_type") else "PUMP3"
        if curve_type == "PUMP1":
            return 0, "Volume [m³]", "Flow [L/s]"
        if curve_type in ("PUMP2", "PUMP4"):
            return 1, "Depth [m]", "Flow [L/s]"
        return 2, "Head [m]", "Flow [L/s]"

    def update_pump_curve_editor(self):
        table = getattr(self, "tbl_pump_curve", None)
        if table is None:
            return
        x_col, x_label, y_label = self.pump_curve_active_columns()
        table.setHorizontalHeaderLabels(["Volume [m³]", "Depth [m]", "Head [m]", "Flow [L/s]"])
        curve_type = self.cmb_pump_curve_type.currentText().strip() if hasattr(self, "cmb_pump_curve_type") else "PUMP3"
        if hasattr(self, "lbl_pump_curve_note"):
            descriptions = {
                "PUMP1": "PUMP1: X = Volume, Y = Flow. Only Volume and Flow are editable.",
                "PUMP2": "PUMP2: X = Depth, Y = Flow. Only Depth and Flow are editable.",
                "PUMP3": "PUMP3: X = Head, Y = Flow. Only Head and Flow are editable.",
                "PUMP4": "PUMP4: X = Depth, Y = Flow. Only Depth and Flow are editable.",
            }
            self.lbl_pump_curve_note.setText(descriptions.get(curve_type, descriptions["PUMP3"]))
        for row in range(table.rowCount()):
            # If the user changes curve type, move the old X value into the new active X column if empty.
            active_item = table.item(row, x_col)
            if active_item is None or not active_item.text().strip():
                for old_col in (0, 1, 2):
                    old_item = table.item(row, old_col)
                    if old_col != x_col and old_item is not None and old_item.text().strip():
                        table.setItem(row, x_col, QTableWidgetItem(old_item.text().strip()))
                        break
            for col in range(4):
                item = table.item(row, col)
                if item is None:
                    item = QTableWidgetItem("")
                    table.setItem(row, col, item)
                if col in (x_col, 3):
                    item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                else:
                    item.setText("")
                    item.setFlags(Qt.ItemIsSelectable)

    def add_pump_curve_row(self, x=None, y=None):
        table = getattr(self, "tbl_pump_curve", None)
        if table is None:
            return
        row = table.rowCount()
        table.insertRow(row)
        x_col, _x_label, _y_label = self.pump_curve_active_columns()
        for col in range(4):
            item = QTableWidgetItem("")
            table.setItem(row, col, item)
        table.item(row, x_col).setText("" if x is None else f"{float(x):.3f}")
        table.item(row, 3).setText("" if y is None else f"{float(y):.3f}")
        self.update_pump_curve_editor()

    def remove_pump_curve_row(self):
        table = getattr(self, "tbl_pump_curve", None)
        if table is None:
            return
        rows = sorted({idx.row() for idx in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            table.removeRow(row)

    def _read_pump_curve_points(self, require_complete=True):
        curve = []
        table = getattr(self, "tbl_pump_curve", None)
        x_col, x_label, y_label = self.pump_curve_active_columns()
        if table is None:
            return curve, x_label, y_label
        for row in range(table.rowCount()):
            x_item = table.item(row, x_col)
            y_item = table.item(row, 3)
            x_raw = x_item.text().strip().replace(",", ".") if x_item else ""
            y_raw = y_item.text().strip().replace(",", ".") if y_item else ""
            if not x_raw and not y_raw:
                continue
            x = _to_float(x_raw, None)
            y = _to_float(y_raw, None)
            if x is None or y is None:
                if require_complete:
                    raise Exception(f"Pump curve non valida alla riga {row + 1}: compila solo {x_label} e {y_label} con valori numerici.")
                continue
            if x < 0:
                raise Exception(f"Pump curve non valida alla riga {row + 1}: {x_label} non può essere negativo.")
            if y < 0:
                raise Exception(f"Pump curve non valida alla riga {row + 1}: {y_label} non può essere negativo.")
            curve.append((float(x), float(y)))
        curve.sort(key=lambda p: p[0])
        return curve, x_label, y_label

    def show_pump_curve_preview(self):
        try:
            curve_type = self.cmb_pump_curve_type.currentText().strip() if hasattr(self, "cmb_pump_curve_type") else "PUMP3"
            curve, x_label, y_label = self._read_pump_curve_points(require_complete=True)
            if len(curve) < 2:
                QMessageBox.warning(self, 'Pump curve chart', f"Inserisci almeno due punti {x_label}/{y_label} per visualizzare la curva.")
                return
            dlg = CurvePreviewDialog(self, f"Pump curve {curve_type} - {x_label} / {y_label}", x_label, y_label, curve)
            dlg.exec_()
        except Exception as e:
            QMessageBox.warning(self, 'Pump curve chart', str(e))

    def get_pump_definitions(self, conduits, nodi):
        if not self.pump_definitions:
            rules_text = self.txt_pump_control_rules.toPlainText().strip() if hasattr(self, "txt_pump_control_rules") else ""
            if rules_text:
                raise Exception('You entered pump control rules, but did not add any pump.')
            return []

        conduit_ids = {str(c.get("cond_id")) for c in conduits}
        node_ids = set(nodi.keys())
        pump_ids = set()
        validated = []
        for p in self.pump_definitions:
            pump_id = str(p.get("pump_id", "")).strip()
            if not pump_id:
                raise Exception('A pump does not have a valid Pump ID.')
            if pump_id in pump_ids:
                raise Exception(f"Pump ID duplicato: {pump_id}.")
            pump_ids.add(pump_id)
            if p.get("replace_link") and str(p["replace_link"]) not in conduit_ids:
                raise Exception(f"La condotta da trasformare in pompa '{p['replace_link']}' non è presente nel modello.")
            if str(p.get("from_node")) not in node_ids or str(p.get("to_node")) not in node_ids:
                raise Exception(f"La pompa '{pump_id}' collega nodi non presenti nel modello.")
            if float(p.get("startup", 0.0)) < float(p.get("shutoff", 0.0)):
                raise Exception(f"Pompa '{pump_id}': startup depth minore della shutoff depth.")
            if str(p.get("curve_type", "PUMP3")) not in ("PUMP1", "PUMP2", "PUMP3", "PUMP4"):
                p["curve_type"] = "PUMP3"
            validated.append(dict(p))

        curve, x_label, y_label = self._read_pump_curve_points(require_complete=True)
        if len(curve) < 2:
            raise Exception(f"La pump curve deve contenere almeno due punti {x_label}/{y_label}.")
        curve.sort(key=lambda p: p[0])
        for i in range(1, len(curve)):
            if curve[i][0] <= curve[i - 1][0]:
                raise Exception(f"La pump curve deve avere valori {x_label} crescenti e non duplicati.")

        rules_lines = []
        rules_text = self.txt_pump_control_rules.toPlainText().strip() if hasattr(self, "txt_pump_control_rules") else ""
        if rules_text:
            rules_lines = [line.rstrip() for line in rules_text.splitlines()]
            if not any(line.strip().upper().startswith("RULE ") for line in rules_lines):
                raise Exception("Pump control rules must contain at least one 'RULE rule_name' line.")

        for p in validated:
            p["curve_points"] = list(curve)
            p["rules"] = rules_lines
        return validated

    def set_collector_workflow_locked(self, locked=True, manual=False):
        """Lock or unlock pipe workflow buttons to prevent accidental reprocessing.

        Steps 1→2→3 remain available in the pipe section, where the active
        alignment is set automatically after drawing a new pipe.
        """
        self.collector_workflow_locked = bool(locked)
        for name in ["btn_nodes_from_trace", "btn_calc_profile", "btn_segments_from_profile"]:
            w = getattr(self, name, None)
            if w is not None:
                w.setEnabled(not self.collector_workflow_locked)
        if hasattr(self, "btn_unlock_collector"):
            self.btn_unlock_collector.setVisible(self.collector_workflow_locked)
        if hasattr(self, "lbl_collector_state"):
            if self.collector_workflow_locked:
                self.lbl_collector_state.setText(
                    "<b>Status:</b> main pipe completed and locked to avoid accidental changes.<br>"
                    "You can still correct the profile from the 'Corrections after creating the pipe' section. "
                    "Use section B to add additional pipes."
                )
                self.lbl_collector_state.setStyleSheet("color: #1b5e20;")
            else:
                self.lbl_collector_state.setText(
                    "<b>Stato:</b> collettore principale modificabile.<br>"
                    "Select the main alignment in the 'Active alignment' field and follow steps 1 → 2 → 3 in section A."
                )
                self.lbl_collector_state.setStyleSheet("")
        if manual:
            self.log_msg("Main pipe manually unlocked for corrections.")

    def refresh_layers(self):
        """Refresh layer lists without changing the user selections.

        Previously, each workflow step rebuilt the combo boxes and QGIS
        automatically selected the first available layer in the list. This could
        change the "Active alignment to process" or the snap layers depending
        on alphabetical/project order. The selected layer ID is now stored before
        refresh and restored if the layer still exists in the project.
        """
        combos_to_preserve = [
            "cmb_condotte",
            "cmb_nodi",
            "cmb_main_edit_pozzetti",
            "cmb_branch_edit_pozzetti",
            "cmb_tracciato",
            "cmb_snap_tratte",
            "cmb_snap_nodi",
            "cmb_dtm",
            "cmb_imperv_roofs",
            "cmb_imperv_roads",
        ]
        previous = {}
        for name in combos_to_preserve:
            combo = getattr(self, name, None)
            if combo is not None:
                previous[name] = combo.currentData()

        def _clear_combo(name):
            combo = getattr(self, name, None)
            if combo is not None:
                combo.blockSignals(True)
                combo.clear()
            return combo

        self.cmb_condotte.clear()
        self.cmb_nodi.clear()
        _clear_combo("cmb_main_edit_pozzetti")
        _clear_combo("cmb_branch_edit_pozzetti")
        _clear_combo("cmb_tracciato")
        _clear_combo("cmb_snap_tratte")
        snap_nodes_combo = _clear_combo("cmb_snap_nodi")
        if snap_nodes_combo is not None:
            snap_nodes_combo.addItem('No node snapping', "")
        _clear_combo("cmb_dtm")
        imperv_roofs_combo = _clear_combo("cmb_imperv_roofs")
        if imperv_roofs_combo is not None:
            imperv_roofs_combo.addItem('No roof layer', "")
        imperv_roads_combo = _clear_combo("cmb_imperv_roads")
        if imperv_roads_combo is not None:
            imperv_roads_combo.addItem('No road layer', "")

        layers = list(QgsProject.instance().mapLayers().values())
        for lyr in layers:
            if isinstance(lyr, QgsVectorLayer):
                if QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.LineGeometry:
                    self.cmb_condotte.addItem(lyr.name(), lyr.id())
                    if hasattr(self, "cmb_tracciato"):
                        self.cmb_tracciato.addItem(lyr.name(), lyr.id())
                    if hasattr(self, "cmb_snap_tratte"):
                        self.cmb_snap_tratte.addItem(lyr.name(), lyr.id())
                elif QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.PointGeometry:
                    self.cmb_nodi.addItem(lyr.name(), lyr.id())
                    if hasattr(self, "cmb_snap_nodi"):
                        self.cmb_snap_nodi.addItem(lyr.name(), lyr.id())
                    if hasattr(self, "cmb_main_edit_pozzetti"):
                        self.cmb_main_edit_pozzetti.addItem(lyr.name(), lyr.id())
                    if hasattr(self, "cmb_branch_edit_pozzetti"):
                        self.cmb_branch_edit_pozzetti.addItem(lyr.name(), lyr.id())
                elif QgsWkbTypes.geometryType(lyr.wkbType()) == QgsWkbTypes.PolygonGeometry:
                    if hasattr(self, "cmb_imperv_roofs"):
                        self.cmb_imperv_roofs.addItem(lyr.name(), lyr.id())
                    if hasattr(self, "cmb_imperv_roads"):
                        self.cmb_imperv_roads.addItem(lyr.name(), lyr.id())
            elif isinstance(lyr, QgsRasterLayer):
                if hasattr(self, "cmb_dtm"):
                    self.cmb_dtm.addItem(lyr.name(), lyr.id())

        # Restore the layers selected by the user. If a layer no longer exists,
        # the combo box remains on the first available item, avoiding errors
        # without changing the other valid selections.
        for name in combos_to_preserve:
            combo = getattr(self, name, None)
            if combo is None:
                continue
            wanted = previous.get(name)
            if wanted not in [None, ""]:
                idx = combo.findData(wanted)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            elif name in ("cmb_snap_nodi", "cmb_imperv_roofs", "cmb_imperv_roads") and combo.count() > 0:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)

    def get_layer(self, combo):
        layer_id = combo.currentData()
        return QgsProject.instance().mapLayer(layer_id)

    def _select_layer_in_combo(self, combo, layer):
        """Select a layer in a combo box robustly, first by ID and then by name."""
        if combo is None or layer is None:
            return False
        idx = combo.findData(layer.id())
        if idx < 0:
            idx = combo.findText(layer.name())
        if idx >= 0:
            combo.setCurrentIndex(idx)
            return True
        return False

    def choose_outdir(self):
        d = QFileDialog.getExistingDirectory(self, 'Select output folder', self.txt_outdir.text())
        if d:
            self.txt_outdir.setText(d)
            if hasattr(self, "lbl_outdir_ref"):
                self.lbl_outdir_ref.setText(d)

    def output_dir_path(self):
        outdir = self.txt_outdir.text().strip() or os.path.expanduser("~")
        os.makedirs(outdir, exist_ok=True)
        return outdir

    def auto_output_path(self, filename):
        """Return the path in the output folder and overwrite existing files when needed."""
        return os.path.join(self.output_dir_path(), filename)

    def choose_python(self):
        f, _ = QFileDialog.getOpenFileName(self, 'Select python.exe', "", 'Python executable (python.exe);;All files (*)')
        if f:
            self.txt_python.setText(f)

    def start_draw_basin(self):
        if self.draw_tool:
            try:
                self.draw_tool.clear_drawing()
            except Exception:
                pass
        self.draw_tool = BasinDrawTool(self.canvas, self.on_basin_finished, self.log_msg)
        self.canvas.setMapTool(self.draw_tool)
        self.log_msg('Draw the catchment with left clicks. Close with right click or Enter.')

    def on_basin_finished(self, geom, crs):
        self.basin_geom = geom
        self.basin_crs = crs
        self.lbl_basin.setText(f"Polygon acquired - map area: {geom.area():.2f}")
        self.canvas.unsetMapTool(self.draw_tool)

    def clear_basin(self):
        self.basin_geom = None
        self.basin_crs = None
        self.lbl_basin.setText('No polygon acquired.')
        if self.draw_tool:
            try:
                self.draw_tool.clear_drawing()
            except Exception:
                self.draw_tool.reset()
        self.log_msg('Catchment polygon cleared.')

    def closeEvent(self, event):
        if self.draw_tool:
            try:
                self.draw_tool.clear_drawing()
            except Exception:
                pass
        if self.trace_draw_tool:
            try:
                self.trace_draw_tool.clear_drawing()
            except Exception:
                pass
        super().closeEvent(event)


    def get_layer_by_combo(self, combo):
        return self.get_layer(combo)

    def first_selected_or_single_line(self, line_layer):
        selected = line_layer.selectedFeatures()
        if len(selected) == 1:
            return selected[0]
        feats = list(line_layer.getFeatures())
        if len(feats) == 1:
            return feats[0]
        raise Exception('Select exactly one alignment polyline, or use a layer with a single geometry.')


    def start_draw_connected_trace(self):
        """Start sketching a proposed sewer connected to the existing computed conduits."""
        try:
            tratte_layer = self.get_layer(self.cmb_snap_tratte)
            if not tratte_layer or QgsWkbTypes.geometryType(tratte_layer.wkbType()) != QgsWkbTypes.LineGeometry:
                raise Exception("In tab 2, select the 'Existing conduits to connect to' layer to connect the new alignment.")
            if self.trace_draw_tool:
                try:
                    self.trace_draw_tool.clear_drawing()
                except Exception:
                    pass
            snap_tol = float(self.spn_snap_tolerance.value())
            node_snap_layer = self.get_layer(self.cmb_snap_nodi) if hasattr(self, "cmb_snap_nodi") else None
            self.trace_draw_tool = ProjectTraceDrawTool(
                self.canvas,
                self.on_connected_trace_finished,
                self.log_msg,
                snap_layer=tratte_layer,
                snap_tolerance=snap_tol,
                snap_node_layer=node_snap_layer
            )
            self.canvas.setMapTool(self.trace_draw_tool)
            self.log_msg(
                'Draw the new sewer: while moving the mouse, the tool displays '
                'the nearest snap point on the existing conduits. Green = within tolerance; '
                'red = outside tolerance. Right-click to finish.'
            )
        except Exception as e:
            self.log_msg(f"ERRORE avvio nuovo tracciato collegato: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def _next_branch_prefix(self):
        """Return A, B, C... based on the pipe layers already present in the project."""
        used = set()
        rx = re.compile(r"tracciato_aggiunto_([A-Z])", re.IGNORECASE)
        for lyr in QgsProject.instance().mapLayers().values():
            m = rx.search(lyr.name())
            if m:
                used.add(m.group(1).upper())
            if isinstance(lyr, QgsVectorLayer):
                names = lyr.fields().names()
                if "branch" in names or "branch_prefix" in names:
                    fld = "branch" if "branch" in names else "branch_prefix"
                    for f in lyr.getFeatures():
                        v = str(f[fld]).strip().upper() if f[fld] not in [None, ""] else ""
                        if len(v) == 1 and v.isalpha():
                            used.add(v)
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if c not in used:
                return c
        return "Z"

    def _line_vertices_xy(self, geom):
        if geom.isMultipart():
            pts = []
            for part in geom.asMultiPolyline() or []:
                pts.extend([QgsPointXY(p) for p in part])
            return pts
        return [QgsPointXY(p) for p in (geom.asPolyline() or [])]

    def _nearest_connection_on_tratte(self, endpoint, tratte_layer):
        """Find the nearest point on the conduits and compute the invert elevation at the connection point."""
        best = None
        pt_geom = QgsGeometry.fromPointXY(endpoint)
        for f in tratte_layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            try:
                near_g = g.nearestPoint(pt_geom)
                if not near_g or near_g.isEmpty():
                    continue
                near_pt = QgsPointXY(near_g.asPoint())
                d = near_g.distance(pt_geom)
                measure = g.lineLocatePoint(near_g)
            except Exception:
                continue

            z_m = self.attr_float(f, ["z_monte", "Z_MONTE"], None)
            z_v = self.attr_float(f, ["z_valle", "Z_VALLE"], None)
            L = self.attr_float(f, ["Lenght", "length", "LENGTH"], None)
            slope = self.attr_float(f, ["Slope", "pendenza", "PENDENZA"], None)
            if L is None or L <= 0:
                L = float(g.length())
            if slope is None and z_m is not None and z_v is not None and L > 0:
                slope = (float(z_m) - float(z_v)) / float(L)
            if slope is None:
                slope = 0.0
            # Compatibility with older layers where slope was stored as a percentage.
            if abs(float(slope)) > 0.5:
                slope = float(slope) / 100.0
            if z_m is None:
                if z_v is not None:
                    z_m = float(z_v) + float(slope) * float(L)
                else:
                    z_m = 0.0
            q_conn = float(z_m) - float(slope) * float(measure)
            candidate = {
                "type": "line",
                "feature": f,
                "point": near_pt,
                "distance": float(d),
                "measure": float(measure),
                "q_scorr": float(q_conn),
                "z_monte": float(z_m),
                "slope": float(slope),
                "length": float(L),
                "cond_id": str(self.attr(f, ["cond_id", "id", "ID", "fid"], f.id())),
            }
            if best is None or candidate["distance"] < best["distance"]:
                best = candidate
        return best

    def _nearest_connection_on_nodes(self, endpoint, nodi_layer):
        """Find the nearest existing node to the sketched endpoint and read its ID and invert elevation."""
        if nodi_layer is None:
            return None
        best = None
        pt_geom = QgsGeometry.fromPointXY(endpoint)
        for f in nodi_layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            try:
                if g.isMultipart():
                    pts = g.asMultiPoint()
                    p = QgsPointXY(pts[0]) if pts else None
                else:
                    p = QgsPointXY(g.asPoint())
            except Exception:
                p = None
            if p is None:
                continue
            d = QgsGeometry.fromPointXY(p).distance(pt_geom)
            node_id = self.attr(f, ["node_id", "NODE_ID", "id", "Id", "ID", "nome", "Name"], f.id())
            q_scorr = self.attr_float(f, ["q_scorr", "Q_SCORR", "quota_fondo", "invert", "invert_elevation"], None)
            elev = self.attr_float(f, ["elevaz", "ground_elevation", "quota_terr", "Quota_T"], None)
            if q_scorr is None and elev is not None:
                prof = self.attr_float(f, ["prof_scav", "excavation_depth", "max_depth", "prof"], None)
                if prof is not None:
                    q_scorr = float(elev) - float(prof)
            if q_scorr is None:
                q_scorr = 0.0
            cand = {
                "type": "node",
                "feature": f,
                "point": p,
                "distance": float(d),
                "measure": 0.0,
                "q_scorr": float(q_scorr),
                "node_id": str(node_id),
                "cond_id": str(node_id),
            }
            if best is None or cand["distance"] < best["distance"]:
                best = cand
        return best

    def on_connected_trace_finished(self, geom_canvas, canvas_crs):
        """Save a new alignment and snap it to the nearest existing conduit."""
        try:
            tratte_layer = self.get_layer(self.cmb_snap_tratte)
            if not tratte_layer or QgsWkbTypes.geometryType(tratte_layer.wkbType()) != QgsWkbTypes.LineGeometry:
                raise Exception('In tab 2, select the existing computed conduit layer to connect the new pipe.')
            node_snap_layer = self.get_layer(self.cmb_snap_nodi) if hasattr(self, "cmb_snap_nodi") else None

            # Save the new alignment in the CRS of the existing conduits.
            # Transform any snap nodes to the same CRS before comparing geometries.
            geom = QgsGeometry(geom_canvas)
            if canvas_crs != tratte_layer.crs():
                tr = QgsCoordinateTransform(canvas_crs, tratte_layer.crs(), QgsProject.instance())
                geom.transform(tr)

            pts = self._line_vertices_xy(geom)
            if len(pts) < 2:
                raise Exception('The new alignment must have at least two vertices.')

            start_info = self._nearest_connection_on_tratte(pts[0], tratte_layer)
            end_info = self._nearest_connection_on_tratte(pts[-1], tratte_layer)

            # If a node layer is provided, also check direct snapping to existing manholes.
            # The new alignment points use the conduit CRS; transform nodes when required.
            start_node_info = None
            end_node_info = None
            if node_snap_layer is not None and QgsWkbTypes.geometryType(node_snap_layer.wkbType()) == QgsWkbTypes.PointGeometry:
                if node_snap_layer.crs() != tratte_layer.crs():
                    pts_for_node = [QgsPointXY(pts[0]), QgsPointXY(pts[-1])]
                    tr_to_nodes = QgsCoordinateTransform(tratte_layer.crs(), node_snap_layer.crs(), QgsProject.instance())
                    p0_node = QgsPointXY(tr_to_nodes.transform(pts_for_node[0]))
                    p1_node = QgsPointXY(tr_to_nodes.transform(pts_for_node[1]))
                    start_node_info = self._nearest_connection_on_nodes(p0_node, node_snap_layer)
                    end_node_info = self._nearest_connection_on_nodes(p1_node, node_snap_layer)
                    tr_to_tratte = QgsCoordinateTransform(node_snap_layer.crs(), tratte_layer.crs(), QgsProject.instance())
                    for ni in (start_node_info, end_node_info):
                        if ni is not None:
                            ni["point"] = QgsPointXY(tr_to_tratte.transform(ni["point"]))
                else:
                    start_node_info = self._nearest_connection_on_nodes(pts[0], node_snap_layer)
                    end_node_info = self._nearest_connection_on_nodes(pts[-1], node_snap_layer)

            candidates = []
            for is_start, info in [(True, start_info), (False, end_info), (True, start_node_info), (False, end_node_info)]:
                if info is not None:
                    info["is_start"] = is_start
                    candidates.append(info)
            if not candidates:
                raise Exception('No existing conduit or node was found to connect the new alignment to.')

            snap_tol = float(self.spn_snap_tolerance.value())
            # Give priority to snapping to an existing node when any node is within
            # tolerance. Intermediate nodes often lie on the same conduit line;
            # without this priority, the conduit would win because the distance
            # to the line can be 0 even when targeting an intermediate manhole.
            node_candidates = [c for c in candidates if c.get("type") == "node" and c.get("distance", 1e99) <= snap_tol]
            if node_candidates:
                conn = min(node_candidates, key=lambda c: c["distance"])
            else:
                conn = min(candidates, key=lambda c: c["distance"])
            use_start = bool(conn.get("is_start"))
            if conn["distance"] > snap_tol:
                raise Exception(
                    f"L'estremo più vicino dista {conn['distance']:.2f} m dalla tratta/nodo esistente, "
                    f"oltre la tolleranza di snap impostata ({snap_tol:.2f} m)."
                )

            # Profile computation runs downstream -> upstream, with the downstream node as the last node.
            # For this reason, the connection point is always moved to the last vertex.
            if use_start:
                pts[0] = conn["point"]
                pts = list(reversed(pts))
            else:
                pts[-1] = conn["point"]
            new_geom = QgsGeometry.fromPolylineXY(pts)
            if new_geom.length() <= 0:
                raise Exception("The new alignment has zero length after snapping.")

            prefix = self.txt_branch_prefix.text().strip().upper()
            if not prefix:
                prefix = self._next_branch_prefix()
            prefix = re.sub(r"[^A-Z0-9]", "", prefix)
            if not prefix:
                prefix = self._next_branch_prefix()
            if len(prefix) > 3:
                prefix = prefix[:3]

            # Shared node between the proposed sewer and the existing network.
            # - when snapping to a conduit, create a new pipe node and split the conduit;
            # - when snapping to an existing node, reuse its ID directly without splitting.
            branch_node_count = self._branch_node_count(new_geom)
            if conn.get("type") == "node" and conn.get("node_id"):
                conn_node_id = str(conn.get("node_id"))
            else:
                conn_node_id = f"{prefix}{branch_node_count}"

            out_path = self.auto_output_path(f"tracciato_aggiunto_{prefix}.gpkg")

            fields = QgsFields()
            for name, typ in [
                ("trace_id", QVariant.String), ("branch", QVariant.String), ("conn_type", QVariant.String), ("conn_cond", QVariant.String),
                ("conn_q", QVariant.Double), ("conn_dist", QVariant.Double), ("conn_meas", QVariant.Double),
                ("conn_x", QVariant.Double), ("conn_y", QVariant.Double), ("snap_tol", QVariant.Double),
                ("conn_node", QVariant.String), ("note", QVariant.String)
            ]:
                fields.append(QgsField(name, typ))
            mem = QgsVectorLayer(f"LineString?crs={tratte_layer.crs().authid()}", f"tracciato_aggiunto_{prefix}", "memory")
            pr = mem.dataProvider(); pr.addAttributes(fields); mem.updateFields()
            of = QgsFeature(mem.fields())
            of.setGeometry(new_geom)
            of["trace_id"] = f"TR_{prefix}"
            of["branch"] = prefix
            of["conn_type"] = conn.get("type", "line")
            of["conn_cond"] = conn["cond_id"]
            of["conn_q"] = round(conn["q_scorr"], 3)
            of["conn_dist"] = round(conn["distance"], 3)
            of["conn_meas"] = round(conn["measure"], 3)
            of["conn_x"] = round(conn["point"].x(), 3)
            of["conn_y"] = round(conn["point"].y(), 3)
            of["snap_tol"] = snap_tol
            of["conn_node"] = conn_node_id
            of["note"] = 'Alignment added; last vertex snapped to an existing node' if conn.get("type") == "node" else 'Alignment added; last vertex snapped to an existing conduit'
            pr.addFeature(of); mem.updateExtents()

            driver = "GPKG" if out_path.lower().endswith(".gpkg") else "ESRI Shapefile"
            QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", mem.crs(), driver)
            lyr = QgsVectorLayer(out_path, f"tracciato_aggiunto_{prefix}", "ogr")
            if not lyr.isValid():
                raise Exception('Error loading the saved new alignment.')
            QgsProject.instance().addMapLayer(lyr)

            # Immediately set the new alignment as the active working alignment.
            # This is required for the 1 -> 2 -> 3 pipe workflow:
            # after sketching, the user must be able to click
            # "1) Create manholes from alignment + DTM" without accidentally processing
            # the main pipe or another pipe again.
            self.refresh_layers()
            self._select_layer_in_combo(self.cmb_tracciato, lyr)
            self.txt_branch_prefix.setText(prefix)
            self.txt_hi.setText(f"{conn['q_scorr']:.3f}")

            # Update the existing network by splitting the snapped conduit and creating
            # the shared connection node in the manhole layer, if available.
            if conn.get("type") == "node":
                self.log_msg(f"New pipe connected to existing node {conn_node_id}: no existing conduit split.")
            else:
                self._split_connected_tratta_and_create_node(tratte_layer, conn, conn_node_id, prefix)

            # Some later operations may refresh the layer list:
            # for safety, reselect the newly created alignment after the split.
            self.refresh_layers()
            self._select_layer_in_combo(self.cmb_tracciato, lyr)
            self.txt_branch_prefix.setText(prefix)

            self.log_msg(
                f"New alignment {prefix} saved: {out_path}. "
                + f"The 'Design alignment' field has automatically been set to 'tracciato_aggiunto_{prefix}'. "
                + (f"Snapped to node {conn_node_id}; " if conn.get("type") == "node" else f"Snapped to {conn['cond_id']} at chainage {conn['measure']:.2f} m; ")
                + f"connection invert elevation = {conn['q_scorr']:.3f} m."
            )
            QMessageBox.information(
                self,
                'New alignment connected',
                'New alignment created successfully.\n\n'
                + f"Node prefix: {prefix}\n"
                + f"Connection type: {'existing node' if conn.get('type') == 'node' else 'existing conduit'}\n"
                + f"Shared connection node: {conn_node_id}\n"
                + f"Connection node invert elevation: {conn['q_scorr']:.3f} m\n\n"
                + (('Connection snapped to an existing node: no conduit split is performed. '
                  "The last node of the new alignment will use the same ID as the existing node. ")
                 if conn.get("type") == "node" else
                 ("The existing conduit has been split at the connection point. "
                  'The new connection node is NOT added to the main pipe manhole layer: '
                  "it will instead be the last node of the new pipe alignment. "))
                + "The 'Design alignment' field has automatically been set to the new pipe. "
                + 'Now use the following sequence: 1) Create manholes from alignment + DEM, '
                + '2) Open the manhole profile editor, and 3) Create conduits from manholes.'
            )
        except Exception as e:
            self.log_msg(f"ERROR creating connected alignment: {e}")
            QMessageBox.critical(self, "Error", str(e))
        finally:
            try:
                self.canvas.unsetMapTool(self.trace_draw_tool)
            except Exception:
                pass


    def _branch_node_count(self, geom):
        """Return the number of nodes that will be generated on the new alignment using the current spacing."""
        line_len = float(geom.length()) if geom and not geom.isEmpty() else 0.0
        interval = float(self.spn_node_interval.value()) if hasattr(self, "spn_node_interval") else 50.0
        if line_len <= 0 or interval <= 0:
            return 1
        eps = 1e-7
        distances = []
        d = 0.0
        while d <= line_len + eps:
            distances.append(min(d, line_len))
            d += interval
        if not distances or abs(distances[-1] - line_len) > eps:
            distances.append(line_len)
        return len(sorted(set(round(x, 8) for x in distances)))

    def _ensure_layer_fields(self, layer, field_defs):
        """Add missing fields to a vector layer."""
        existing = set(layer.fields().names())
        missing = [QgsField(n, t) for n, t in field_defs if n not in existing]
        if missing:
            layer.dataProvider().addAttributes(missing)
            layer.updateFields()

    def _copy_attrs_to_feature(self, target_feat, source_feat):
        """Copy compatible attributes by field name."""
        src_names = source_feat.fields().names()
        for fld in target_feat.fields():
            n = fld.name()
            if n in src_names:
                try:
                    target_feat[n] = source_feat[n]
                except Exception:
                    pass

    def _set_if_field(self, feat, name, value):
        if name in feat.fields().names():
            feat[name] = value

    def _clear_provider_primary_key_attrs(self, layer, feat):
        """
        Avoid GeoPackage errors such as UNIQUE constraint failures on fid/ogc_fid.
        When duplicating a feature to split a conduit, the physical record
        identifier must not be copied because it must be reassigned by the provider.
        """
        try:
            feat.setId(-1)
        except Exception:
            pass
        pk_indexes = []
        try:
            pk_indexes = list(layer.dataProvider().pkAttributeIndexes())
        except Exception:
            pk_indexes = []
        names = feat.fields().names()
        fallback_pk_names = {"fid", "ogc_fid", "objectid", "object_id", "gid"}
        for i, name in enumerate(names):
            try:
                if i in pk_indexes or str(name).lower() in fallback_pk_names:
                    feat[name] = None
            except Exception:
                pass

    def _unique_field_value(self, layer, field_name, wanted_value):
        """Return a value that is not already present in the specified field."""
        try:
            names = layer.fields().names()
            if field_name not in names:
                return wanted_value
            existing = set()
            for ft in layer.getFeatures():
                try:
                    v = ft[field_name]
                    if v is not None and str(v) != "":
                        existing.add(str(v))
                except Exception:
                    pass
            base = str(wanted_value)
            if base not in existing:
                return base
            n = 1
            while f"{base}_{n}" in existing:
                n += 1
            return f"{base}_{n}"
        except Exception:
            return wanted_value

    def _split_connected_tratta_and_create_node(self, tratte_layer, conn, conn_node_id, prefix):
        """
        Split the existing conduit intersected by the proposed sewer in place.
        Create two replacement conduits with the intermediate connection node
        conn_node_id and, when available, add the node to the selected manhole
        layer.
        """
        try:
            src_feat = conn.get("feature")
            if src_feat is None:
                self.log_msg('Conduit split: connection feature is not available.')
                return

            g = src_feat.geometry()
            measure = float(conn.get("measure", 0.0))
            total_len = float(g.length()) if g and not g.isEmpty() else 0.0
            if total_len <= 0:
                self.log_msg("Conduit split: existing conduit length is zero.")
                return
            if measure <= 0.01 or measure >= total_len - 0.01:
                self.log_msg('Conduit split: the connection is too close to an endpoint; the conduit will not be split. The new node will still be created in the manhole layer of the new pipe alignment, not in the main pipe.')
                return

            self._ensure_layer_fields(tratte_layer, [
                ("cond_id", QVariant.String), ("id_monte", QVariant.String), ("id_valle", QVariant.String),
                ("pk_monte", QVariant.Double), ("pk_valle", QVariant.Double),
                ("z_monte", QVariant.Double), ("z_valle", QVariant.Double),
                ("length", QVariant.Double), ("Lenght", QVariant.Double),
                ("pendenza", QVariant.Double), ("Slope", QVariant.Double),
                ("materiale", QVariant.String), ("diam_m", QVariant.Double),
                ("split_da", QVariant.String), ("split_node", QVariant.String)
            ])

            old_cond = str(self.attr(src_feat, ["cond_id", "id", "ID", "fid"], src_feat.id()))
            old_from = str(self.attr(src_feat, ["id_monte", "from_node", "FROM_NODE"], ""))
            old_to = str(self.attr(src_feat, ["id_valle", "to_node", "TO_NODE"], ""))
            z_m = self.attr_float(src_feat, ["z_monte", "Z_MONTE"], conn.get("z_monte"))
            L_attr = self.attr_float(src_feat, ["Lenght", "length", "LENGTH"], total_len)
            slope_old = self.attr_float(src_feat, ["Slope", "pendenza", "PENDENZA"], conn.get("slope", 0.0))
            if slope_old is None:
                slope_old = 0.0
            if abs(float(slope_old)) > 0.5:
                slope_old = float(slope_old) / 100.0
            z_v = self.attr_float(src_feat, ["z_valle", "Z_VALLE"], None)
            if z_m is None and z_v is not None:
                z_m = float(z_v) + float(slope_old) * float(L_attr or total_len)
            if z_m is None:
                z_m = float(conn.get("q_scorr", 0.0)) + float(slope_old) * measure

            # Invert elevation of the new connection node computed from the original conduit,
            # using the original upstream invert elevation and original slope:
            # q_conn = original_upstream_invert - original_slope * upstream_length.
            q_conn = float(z_m) - float(slope_old) * float(measure)

            geom1 = self.extract_subline(g, 0.0, measure)
            geom2 = self.extract_subline(g, measure, total_len)
            if geom1 is None or geom1.isEmpty() or geom2 is None or geom2.isEmpty():
                self.log_msg('Conduit split: unable to create one of the two sub-conduits.')
                return
            len1 = float(geom1.length())
            len2 = float(geom2.length())

            # The two replacement conduits preserve the slope of the original conduit;
            # z_monte/z_valle are recomputed using the new geometric lengths.
            slope1 = float(slope_old)
            slope2 = float(slope_old)
            z_v1 = float(z_m) - slope1 * len1
            z_m2 = z_v1
            z_v2 = z_m2 - slope2 * len2
            q_conn = z_v1

            f1 = QgsFeature(tratte_layer.fields())
            f2 = QgsFeature(tratte_layer.fields())
            self._copy_attrs_to_feature(f1, src_feat)
            self._copy_attrs_to_feature(f2, src_feat)
            self._clear_provider_primary_key_attrs(tratte_layer, f1)
            self._clear_provider_primary_key_attrs(tratte_layer, f2)
            f1.setGeometry(geom1)
            f2.setGeometry(geom2)

            cond1 = self._unique_field_value(tratte_layer, "cond_id", f"{old_cond}_1")
            cond2 = self._unique_field_value(tratte_layer, "cond_id", f"{old_cond}_2")
            if cond2 == cond1:
                cond2 = self._unique_field_value(tratte_layer, "cond_id", f"{old_cond}_2b")

            for feat, cond_new, from_id, to_id, pk_m, pk_v, zz_m, zz_v, ll, ss in [
                (f1, cond1, old_from, conn_node_id, self.attr_float(src_feat, ["pk_monte"], 0.0), self.attr_float(src_feat, ["pk_monte"], 0.0) + len1, z_m, z_v1, len1, slope1),
                (f2, cond2, conn_node_id, old_to, self.attr_float(src_feat, ["pk_monte"], 0.0) + len1, self.attr_float(src_feat, ["pk_monte"], 0.0) + len1 + len2, z_m2, z_v2, len2, slope2),
            ]:
                self._set_if_field(feat, "cond_id", cond_new)
                self._set_if_field(feat, "id_monte", str(from_id))
                self._set_if_field(feat, "id_valle", str(to_id))
                self._set_if_field(feat, "pk_monte", round(float(pk_m or 0.0), 3))
                self._set_if_field(feat, "pk_valle", round(float(pk_v or 0.0), 3))
                self._set_if_field(feat, "z_monte", round(float(zz_m), 3))
                self._set_if_field(feat, "z_valle", round(float(zz_v), 3))
                self._set_if_field(feat, "length", round(float(ll), 3))
                self._set_if_field(feat, "Lenght", round(float(ll), 3))
                self._set_if_field(feat, "pendenza", round(float(ss), 5))
                self._set_if_field(feat, "Slope", round(float(ss), 5))
                self._set_if_field(feat, "split_da", old_cond)
                self._set_if_field(feat, "split_node", conn_node_id)

            was_edit = tratte_layer.isEditable()
            if not was_edit:
                tratte_layer.startEditing()
            if not tratte_layer.deleteFeature(src_feat.id()):
                raise Exception("Failed to delete the original conduit.")
            ok1 = tratte_layer.addFeature(f1)
            ok2 = tratte_layer.addFeature(f2)
            if not ok1 or not ok2:
                try:
                    errs = tratte_layer.dataProvider().errors()
                except Exception:
                    errs = []
                raise Exception('Failed to create the split conduits. ' + ("; ".join([str(e) for e in errs]) if errs else ""))
            if not was_edit:
                if not tratte_layer.commitChanges():
                    errs = []
                    try:
                        errs = tratte_layer.commitErrors()
                    except Exception:
                        pass
                    raise Exception('Failed to commit the conduit split to the conduit layer. ' + ("; ".join([str(e) for e in errs]) if errs else ""))
            tratte_layer.triggerRepaint()
            self.log_msg(f"Conduit {old_cond} split at node {conn_node_id}: created {cond1} and {cond2}. Node {conn_node_id} will be part of the new pipe alignment.")
        except Exception as e:
            self.log_msg(f"ERRORE split tratta collegata: {e}")
            QMessageBox.warning(self, "Warning", f"Nuovo tracciato creato, ma lo split della existing conduit non è riuscito:\n{e}")

    def _add_connection_node_to_node_layer(self, conn, conn_node_id, prefix):
        """Add the connection node to the selected node layer, when available."""
        try:
            node_layer = self.get_layer(self.cmb_nodi)
            if not node_layer or QgsWkbTypes.geometryType(node_layer.wkbType()) != QgsWkbTypes.PointGeometry:
                self.log_msg('Connection node: no valid manhole layer selected; node creation in the node layer will be skipped.')
                return
            self._ensure_layer_fields(node_layer, [
                ("node_id", QVariant.String), ("q_scorr", QVariant.Double), ("invert_elevation", QVariant.Double),
                ("ground_elevation", QVariant.Double), ("elevaz", QVariant.Double),
                ("prof_scav", QVariant.Double), ("excavation_depth", QVariant.Double),
                ("branch", QVariant.String), ("is_conn", QVariant.Int)
            ])
            # Avoid duplicates if the node already exists.
            names = node_layer.fields().names()
            if "node_id" in names:
                for f in node_layer.getFeatures():
                    if str(f["node_id"]).strip() == str(conn_node_id):
                        self.log_msg(f"Nodo collegamento {conn_node_id} già presente nel layer nodi.")
                        return
            f = QgsFeature(node_layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(conn["point"]))
            self._set_if_field(f, "node_id", conn_node_id)
            self._set_if_field(f, "q_scorr", round(float(conn["q_scorr"]), 3))
            self._set_if_field(f, "invert_elevation", round(float(conn["q_scorr"]), 3))
            self._set_if_field(f, "branch", prefix)
            self._set_if_field(f, "is_conn", 1)
            was_edit = node_layer.isEditable()
            if not was_edit:
                node_layer.startEditing()
            node_layer.addFeature(f)
            if not was_edit:
                if not node_layer.commitChanges():
                    raise Exception('Failed to commit the connection node.')
            node_layer.triggerRepaint()
            self.log_msg(f"Nodo di collegamento {conn_node_id} creato nel layer pozzetti con q_scorr={conn['q_scorr']:.3f} m.")
        except Exception as e:
            self.log_msg(f"ERRORE creazione nodo collegamento: {e}")
    def create_nodes_from_trace_dtm(self):
        """Create manholes along the alignment at a fixed spacing and sample ground elevations from the DTM."""
        try:
            trace_layer = self.get_layer(self.cmb_tracciato)
            dtm_layer = QgsProject.instance().mapLayer(self.cmb_dtm.currentData()) if hasattr(self, "cmb_dtm") else None
            if not trace_layer or QgsWkbTypes.geometryType(trace_layer.wkbType()) != QgsWkbTypes.LineGeometry:
                raise Exception('Select a valid line layer as the design alignment.')
            if not dtm_layer or not isinstance(dtm_layer, QgsRasterLayer):
                raise Exception('Select a valid DEM/raster layer.')

            line_feat = self.first_selected_or_single_line(trace_layer)
            line_geom = line_feat.geometry()
            if not line_geom or line_geom.isEmpty() or line_geom.length() <= 0:
                raise Exception("The selected polyline has zero or invalid length.")

            interval = float(self.spn_node_interval.value())
            line_len = float(line_geom.length())
            trace_fields = line_feat.fields().names()
            branch_prefix = None
            conn_q = None
            if "branch" in trace_fields and line_feat["branch"] not in [None, ""]:
                branch_prefix = str(line_feat["branch"]).strip().upper()
            elif "branch_prefix" in trace_fields and line_feat["branch_prefix"] not in [None, ""]:
                branch_prefix = str(line_feat["branch_prefix"]).strip().upper()
            conn_node_override = None
            if "conn_q" in trace_fields and line_feat["conn_q"] not in [None, ""]:
                conn_q = float(line_feat["conn_q"])
            elif "conn_q_sc" in trace_fields and line_feat["conn_q_sc"] not in [None, ""]:
                conn_q = float(line_feat["conn_q_sc"])
            if "conn_node" in trace_fields and line_feat["conn_node"] not in [None, ""]:
                conn_node_override = str(line_feat["conn_node"]).strip()
            conn_type = ""
            if "conn_type" in trace_fields and line_feat["conn_type"] not in [None, ""]:
                conn_type = str(line_feat["conn_type"]).strip().lower()
            conn_to_existing_node = bool(conn_type == "node" and conn_node_override)

            default_name = f"pozzetti_da_tracciato_{branch_prefix}.gpkg" if branch_prefix else "pozzetti_da_tracciato.gpkg"
            out_path = self.auto_output_path(default_name)

            # Manhole creation distances along the alignment.
            # Logica richiesta:
            # - first manhole always on the first vertex / pk = 0;
            # - manholes at each spacing interval defined by the user;
            # - the last manhole is ALWAYS placed on the actual end point of the alignment,
            #   even when the alignment length is not a multiple of the interval.
            eps = 1e-7
            distances = [0.0]
            d = interval
            while d < line_len - eps:
                distances.append(float(d))
                d += interval

            # Always append the actual final point of the geometry.
            # Even when the pipe is snapped to an existing node, the terminal node
            # must be present in the pipe manhole layer to allow profile editing and
            # allow the profile editor to reach the connection node and display
            # the related invert elevation/depth definition.
            # Duplication in the SWMM model is avoided during input preparation by
            # aggregating manholes with the same node_id.
            if abs(distances[-1] - line_len) <= eps:
                distances[-1] = float(line_len)
            else:
                distances.append(float(line_len))

            fields = QgsFields()
            fields.append(QgsField("Id", QVariant.Int))
            fields.append(QgsField("node_id", QVariant.String))
            fields.append(QgsField("ground_elevation", QVariant.Double))
            fields.append(QgsField("elevaz", QVariant.Double))
            fields.append(QgsField("Distance", QVariant.Double))
            fields.append(QgsField("pk", QVariant.Double))
            fields.append(QgsField("q_scorr", QVariant.Double))
            fields.append(QgsField("invert_elevation", QVariant.Double))
            fields.append(QgsField("prof_scav", QVariant.Double))
            fields.append(QgsField("is_conn", QVariant.Int))
            fields.append(QgsField("branch", QVariant.String))

            mem_name = f"pozzetti_da_tracciato_{branch_prefix}" if branch_prefix else "pozzetti_da_tracciato"
            mem = QgsVectorLayer(f"Point?crs={trace_layer.crs().authid()}", mem_name, "memory")
            pr = mem.dataProvider()
            pr.addAttributes(fields)
            mem.updateFields()

            raster_provider = dtm_layer.dataProvider()
            prev = None
            count = 0
            for i, dist in enumerate(distances, start=1):
                pt_geom = line_geom.interpolate(dist)
                if not pt_geom or pt_geom.isEmpty():
                    continue
                pt = pt_geom.asPoint()
                # If the DTM and alignment use different CRS, transform the point to the raster CRS.
                sample_pt = QgsPointXY(pt)
                if trace_layer.crs() != dtm_layer.crs():
                    tr = QgsCoordinateTransform(trace_layer.crs(), dtm_layer.crs(), QgsProject.instance())
                    sample_pt = tr.transform(sample_pt)
                ident = raster_provider.identify(sample_pt, QgsRaster.IdentifyFormatValue)
                elev_val = None
                if ident.isValid() and ident.results():
                    try:
                        elev_val = float(list(ident.results().values())[0])
                    except Exception:
                        elev_val = None

                dist_parz = 0.0 if prev is None else max(float(dist) - float(prev), 0.0)
                f = QgsFeature(mem.fields())
                f.setGeometry(pt_geom)
                f["Id"] = 1
                is_connection = 1 if (conn_q is not None and abs(float(dist) - float(line_len)) <= max(eps, 1e-6)) else 0
                if is_connection and conn_node_override:
                    node_name = conn_node_override
                else:
                    node_name = f"{branch_prefix}{i}" if branch_prefix else str(i)
                f["node_id"] = node_name
                f["ground_elevation"] = elev_val
                f["elevaz"] = elev_val
                f["Distance"] = dist_parz
                f["pk"] = float(dist)
                f["is_conn"] = is_connection
                f["branch"] = branch_prefix or ""
                if is_connection:
                    f["q_scorr"] = float(conn_q)
                    f["invert_elevation"] = float(conn_q)
                    if elev_val is not None:
                        f["prof_scav"] = float(elev_val) - float(conn_q)
                pr.addFeature(f)
                prev = dist
                count += 1

            mem.updateExtents()
            driver = "GPKG" if out_path.lower().endswith(".gpkg") else "ESRI Shapefile"
            QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", mem.crs(), driver)
            lyr_name = f"pozzetti_da_tracciato_{branch_prefix}" if branch_prefix else "pozzetti_da_tracciato"
            lyr = QgsVectorLayer(out_path, lyr_name, "ogr")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                self.refresh_layers()
                idx = self.cmb_nodi.findData(lyr.id())
                if idx >= 0:
                    self.cmb_nodi.setCurrentIndex(idx)
            if conn_to_existing_node:
                self.log_msg(f"Snapped to existing node {conn_node_override}: the terminal manhole has been added to the pipe profile with the same node_id, without splitting the existing conduit.")
            self.log_msg(f"Creati {count} pozzetti da tracciato + DTM: {out_path}")
            msg_extra = (f"\n\nSnapped to existing node {conn_node_override}: the pipe profile reaches the existing node. In SWMM, nodes with the same ID are handled as a single node." if conn_to_existing_node else "")
            QMessageBox.information(self, "Sewer Builder", f"Pozzetti creati correttamente:\n{out_path}" + msg_extra)
        except Exception as e:
            self.log_msg(f"ERRORE creazione pozzetti da tracciato: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def edit_existing_profile_from_combo(self, combo):
        """Open the profile editor using the computed manhole layer selected in the correction section."""
        try:
            lyr = self.get_layer(combo)
            if not lyr or QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.PointGeometry:
                raise Exception('Select a valid manhole_calculated point layer in the correction section.')
            if not self._select_layer_in_combo(self.cmb_nodi, lyr):
                self.refresh_layers()
                self._select_layer_in_combo(self.cmb_nodi, lyr)
            self.log_msg(f"Apro editor profilo esistente: {lyr.name()}")
            self.calculate_profile_from_nodes(allow_manual_nodes=False)
        except Exception as e:
            self.log_msg(f"ERRORE apertura profilo esistente: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def regen_segments_from_profile_combo(self, combo):
        """Regenerate conduits using the computed manhole layer selected in the correction section."""
        try:
            lyr = self.get_layer(combo)
            if not lyr or QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.PointGeometry:
                raise Exception('Select a valid manhole_calculated point layer in the correction section.')
            if not self._select_layer_in_combo(self.cmb_nodi, lyr):
                self.refresh_layers()
                self._select_layer_in_combo(self.cmb_nodi, lyr)
            self.log_msg(f"Rigenero tratte dal profilo esistente: {lyr.name()}")
            self.create_segments_from_profile_nodes()
        except Exception as e:
            self.log_msg(f"ERRORE rigenerazione tratte da profilo esistente: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def calculate_profile_from_nodes(self, allow_manual_nodes=True):
        """Open the full longitudinal profile editor integrated in QGIS.

        The editor preserves the workflow of the desktop Sewer Builder application:
        downstream-to-upstream computation, slope editing, invert drops, diameter
        upgrades, undo operations, checks and output generation.
        """
        try:
            nodi_layer = self.get_layer(self.cmb_nodi)
            if not nodi_layer or QgsWkbTypes.geometryType(nodi_layer.wkbType()) != QgsWkbTypes.PointGeometry:
                raise Exception('Select the manhole point layer to compute/edit.')
            outdir = self.txt_outdir.text().strip() or os.path.expanduser("~")
            dlg = ProfileEditorDialog(self, nodi_layer, outdir, self, allow_manual_nodes=allow_manual_nodes)
            # The editor must be non-modal: this allows the "Add node on map"
            # button to activate an actual map tool on the QGIS canvas. With
            # exec_(), the dialog remains modal and the canvas does not receive
            # mouse clicks.
            dlg.setModal(False)
            dlg.setWindowModality(Qt.NonModal)
            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            self._profile_editor_dialog = dlg
            dlg.show()
            dlg.raise_()
        except Exception as e:
            self.log_msg(f"ERRORE apertura editor profilo: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def extract_subline(self, geom_line, start_m, end_m):
        if start_m == end_m:
            return None
        reverse = False
        if start_m > end_m:
            start_m, end_m = end_m, start_m
            reverse = True
        pts = []
        if geom_line.isMultipart():
            for part in geom_line.asMultiPolyline() or []:
                pts.extend([QgsPointXY(p) for p in part])
        else:
            pts = [QgsPointXY(p) for p in (geom_line.asPolyline() or [])]
        if len(pts) < 2:
            return None
        total_len = geom_line.length()
        start_m = max(0.0, min(float(start_m), total_len))
        end_m = max(0.0, min(float(end_m), total_len))
        out_pts = []
        cur_m = 0.0
        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
            seg_len = math.hypot(dx, dy)
            if seg_len <= 0:
                continue
            next_m = cur_m + seg_len
            if next_m < start_m:
                cur_m = next_m
                continue
            if cur_m > end_m:
                break
            local_start = max(start_m, cur_m)
            local_end = min(end_m, next_m)
            t0 = (local_start - cur_m) / seg_len
            t1 = (local_end - cur_m) / seg_len
            sp = QgsPointXY(p1.x() + dx * t0, p1.y() + dy * t0)
            ep = QgsPointXY(p1.x() + dx * t1, p1.y() + dy * t1)
            if not out_pts or out_pts[-1] != sp:
                out_pts.append(sp)
            out_pts.append(ep)
            cur_m = next_m
            if next_m >= end_m:
                break
        if len(out_pts) < 2:
            return None
        if reverse:
            out_pts.reverse()
        return QgsGeometry.fromPolylineXY(out_pts)

    def _branch_prefix_from_trace(self, trace_layer, line_feat=None):
        """Extract the sewer prefix from the alignment layer or feature, when available.

        The prefix is used to generate readable filenames for the outputs created
        by command 3) Create conduits from manholes, for example
        pipe_calculated_A.gpkg, pipe_calculated_B.gpkg, and so on.
        """
        try:
            if line_feat is not None:
                names = line_feat.fields().names()
                for fld in ["branch", "branch_prefix", "prefisso"]:
                    if fld in names and line_feat[fld] not in [None, ""]:
                        v = re.sub(r"[^A-Za-z0-9]", "", str(line_feat[fld]).strip().upper())
                        if v:
                            return v[:10]
        except Exception:
            pass
        try:
            nm = str(trace_layer.name())
            for pat in [r"tracciato_aggiunto_([A-Za-z0-9]+)", r"pozzetti_.*_([A-Za-z0-9]+)$", r"ramo_([A-Za-z0-9]+)"]:
                m = re.search(pat, nm, re.IGNORECASE)
                if m:
                    v = re.sub(r"[^A-Za-z0-9]", "", m.group(1).strip().upper())
                    if v:
                        return v[:10]
        except Exception:
            pass
        return ""

    def create_segments_from_profile_nodes(self):
        """Create linear conduits from the alignment and computed manholes, including upstream/downstream node IDs, invert elevations and slope fields."""
        try:
            trace_layer = self.get_layer(self.cmb_tracciato)
            node_layer = self.get_layer(self.cmb_nodi)
            if not trace_layer or QgsWkbTypes.geometryType(trace_layer.wkbType()) != QgsWkbTypes.LineGeometry:
                raise Exception('Select the design line alignment.')
            if not node_layer or QgsWkbTypes.geometryType(node_layer.wkbType()) != QgsWkbTypes.PointGeometry:
                raise Exception('Select the manhole_calculated layer.')
            line_feat = self.first_selected_or_single_line(trace_layer)
            line_geom = line_feat.geometry()

            branch_prefix = self._branch_prefix_from_trace(trace_layer, line_feat)
            if branch_prefix:
                out_name = f"pipe_calculated_{branch_prefix}.gpkg"
                layer_result_name = f"pipe_calculated_{branch_prefix}"
            else:
                out_name = "pipe_calculated.gpkg"
                layer_result_name = "pipe_calculated"
            out_path = self.auto_output_path(out_name)

            # Aggregate manholes by node_id.
            # This is required to handle invert drops, which are represented in the profile as
            # represented by two rows/points with the same node_id and pk:
            # - z_top    = higher conduit inlet/outlet invert elevation before the drop;
            # - z_bottom = manhole bottom invert elevation after the drop.
            #
            # When creating conduits:
            # - at the upstream node, the conduit starts from z_bottom;
            # - at the downstream node, the conduit reaches z_top.
            # This allows a pipe to enter the receiving pipe above the connection
            # manhole bottom, resulting in a positive SWMM offset.
            node_map = {}
            for f in node_layer.getFeatures():
                pk = self.attr_float(f, ["pk", "PK"], None)
                if pk is None:
                    continue
                nid = self.attr(f, ["node_id", "NODE_ID", "Id", "ID", "id"])
                z = self.attr_float(f, ["q_scorr", "invert_elevation", "quota_fondo"], None)
                if nid is None or z is None:
                    continue
                nid = str(nid)
                z = float(z)
                if nid not in node_map:
                    node_map[nid] = {
                        "node_id": nid,
                        "pk": float(pk),
                        "z_top": z,
                        "z_bottom": z,
                        "materiale": self.attr(f, ["materiale", "MATERIALE"]),
                        "diam_m": self.attr_float(f, ["diam_m", "D", "diametro"], None)
                    }
                else:
                    node_map[nid]["pk"] = min(float(node_map[nid]["pk"]), float(pk))
                    node_map[nid]["z_top"] = max(float(node_map[nid]["z_top"]), z)
                    node_map[nid]["z_bottom"] = min(float(node_map[nid]["z_bottom"]), z)
                    if node_map[nid].get("materiale") in [None, ""]:
                        node_map[nid]["materiale"] = self.attr(f, ["materiale", "MATERIALE"])
                    if node_map[nid].get("diam_m") in [None, ""]:
                        node_map[nid]["diam_m"] = self.attr_float(f, ["diam_m", "D", "diametro"], None)
            nodes = list(node_map.values())
            nodes.sort(key=lambda d: d["pk"])

            # If the pipe alignment snaps to an existing node, that node is NOT
            # created in the pipe manhole layer. However, it is still required as a
            # virtual terminal node to create the last pipe conduit toward the existing node.
            trace_fields = line_feat.fields().names()
            conn_type = str(line_feat["conn_type"]).strip().lower() if "conn_type" in trace_fields and line_feat["conn_type"] not in [None, ""] else ""
            conn_node = str(line_feat["conn_node"]).strip() if "conn_node" in trace_fields and line_feat["conn_node"] not in [None, ""] else ""
            conn_q = None
            if "conn_q" in trace_fields and line_feat["conn_q"] not in [None, ""]:
                try:
                    conn_q = float(line_feat["conn_q"])
                except Exception:
                    conn_q = None
            if conn_type == "node" and conn_node and conn_q is not None:
                line_len = float(line_geom.length())
                if not any(str(n.get("node_id")) == conn_node for n in nodes):
                    mat = nodes[-1].get("materiale") if nodes else ""
                    diam = nodes[-1].get("diam_m") if nodes else None
                    nodes.append({
                        "node_id": conn_node,
                        "pk": line_len,
                        "z_top": float(conn_q),
                        "z_bottom": float(conn_q),
                        "materiale": mat,
                        "diam_m": diam,
                        "virtual_existing_node": True,
                    })
                    nodes.sort(key=lambda d: d["pk"])
                    self.log_msg(f"Snapped to existing node {conn_node}: added only as a virtual node to create the terminal conduit, without duplicating it in the pipe manhole layer.")

            if len(nodes) < 2:
                raise Exception('At least 2 nodes with pk and q_scorr are required to create conduits.')

            fields = QgsFields()
            for name, typ in [
                ("cond_id", QVariant.String), ("line_id", QVariant.Int), ("id_monte", QVariant.String), ("id_valle", QVariant.String),
                ("pk_monte", QVariant.Double), ("pk_valle", QVariant.Double), ("z_monte", QVariant.Double), ("z_valle", QVariant.Double),
                ("length", QVariant.Double), ("Lenght", QVariant.Double), ("pendenza", QVariant.Double), ("Slope", QVariant.Double),
                ("materiale", QVariant.String), ("diam_m", QVariant.Double)
            ]:
                fields.append(QgsField(name, typ))
            mem = QgsVectorLayer(f"LineString?crs={trace_layer.crs().authid()}", layer_result_name, "memory")
            pr = mem.dataProvider(); pr.addAttributes(fields); mem.updateFields()

            count = 0
            for i in range(len(nodes) - 1):
                n1, n2 = nodes[i], nodes[i + 1]
                geom = self.extract_subline(line_geom, n1["pk"], n2["pk"])
                if geom is None or geom.isEmpty():
                    continue
                seg_len = geom.length()
                if seg_len <= 0:
                    continue
                z_monte = float(n1.get("z_bottom", n1.get("z_top")))
                z_valle = float(n2.get("z_top", n2.get("z_bottom")))
                slope = (z_monte - z_valle) / seg_len  # m/m
                of = QgsFeature(mem.fields())
                of.setGeometry(geom)
                of["cond_id"] = f"COND_{n1['node_id']}_{n2['node_id']}"
                of["line_id"] = int(line_feat.id())
                of["id_monte"] = n1["node_id"]
                of["id_valle"] = n2["node_id"]
                of["pk_monte"] = round(n1["pk"], 3)
                of["pk_valle"] = round(n2["pk"], 3)
                of["z_monte"] = round(z_monte, 3)
                of["z_valle"] = round(z_valle, 3)
                of["length"] = round(float(seg_len), 3)
                of["Lenght"] = round(float(seg_len), 3)
                of["pendenza"] = round(float(slope), 5)
                of["Slope"] = round(float(slope), 5)
                of["materiale"] = str(n1.get("materiale") or "")
                of["diam_m"] = float(n1["diam_m"]) if n1.get("diam_m") not in [None, ""] else None
                pr.addFeature(of)
                count += 1
            mem.updateExtents()
            driver = "GPKG" if out_path.lower().endswith(".gpkg") else "ESRI Shapefile"
            # When correcting the profile and regenerating conduits, the file must be
            # replaced rather than appended to or left with old segments.
            # If Windows/QGIS keeps the file in use, writing may fail; in that case,
            # a clear error is shown so the user can remove the old layer.
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except Exception:
                    self.log_msg(f"ATTENZIONE: impossibile eliminare il vecchio file tratte {out_path}. Chiudi/rimuovi il layer dal progetto se la rigenerazione non aggiorna il file.")
            QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", mem.crs(), driver)
            lyr = QgsVectorLayer(out_path, layer_result_name, "ogr")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                self.refresh_layers()
                idx = self.cmb_condotte.findData(lyr.id())
                if idx >= 0:
                    self.cmb_condotte.setCurrentIndex(idx)
            self.log_msg(f"Created {count} computed conduits ({layer_result_name}): {out_path}")
            if not branch_prefix:
                self.set_collector_workflow_locked(True)
                self.log_msg('Main pipe completed: section A has been locked. Use section B for additional pipes.')
            QMessageBox.information(self, "Sewer Builder", f"Conduits created successfully:\n{out_path}")
        except Exception as e:
            self.log_msg(f"ERROR creating conduits: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def create_pozzetti_file(self):
        'Create a computed manhole layer compatible with the Sewer Builder workflow.\n\n        The function uses:\n        - node layer: point geometry and, when available, elevaz / q_scorr / prof_scav;\n        - conduit layer: id_monte, id_valle, z_monte, z_valle, Slope, Lenght,\n          material and diameter.\n\n        For each node, the main fields used by the SWMM model are created or\n        updated: node_id, elevaz, q_scorr and prof_scav. Additional Sewer Builder\n        profile fields are also added: pk, Distanza, slope, materiale, diam_m\n        and estradosso.\n        '
        try:
            cond_layer = self.get_layer(self.cmb_condotte)
            nodi_layer = self.get_layer(self.cmb_nodi)
            if not cond_layer or not nodi_layer:
                raise Exception('Select a conduit layer and a node/manhole layer first.')

            out_path = self.auto_output_path("manhole_calculated_swmm.gpkg")

            self.log.clear()
            self.log_msg('Creating node/manhole file according to Sewer Builder logic...')

            conduits_info = self.collect_conduit_info_for_pozzetti(cond_layer)
            pk_map = self.compute_pk_map(conduits_info)

            out_fields = QgsFields()
            for name, typ in [
                ("node_id", QVariant.String),
                ("elevaz", QVariant.Double),
                ("q_scorr", QVariant.Double),
                ("prof_scav", QVariant.Double),
                ("pk", QVariant.Double),
                ("Distance", QVariant.Double),
                ("pendenza", QVariant.Double),
                ("materiale", QVariant.String),
                ("diam_m", QVariant.Double),
                ("estradosso", QVariant.Double),
                ("id_monte", QVariant.String),
                ("id_valle", QVariant.String),
                ("note", QVariant.String),
            ]:
                out_fields.append(QgsField(name, typ))

            mem = QgsVectorLayer(f"Point?crs={nodi_layer.crs().authid()}", "manhole_calculated_swmm", "memory")
            pr = mem.dataProvider()
            pr.addAttributes(out_fields)
            mem.updateFields()

            count = 0
            warnings = 0

            for nf in nodi_layer.getFeatures():
                geom = nf.geometry()
                if not geom or geom.isEmpty():
                    continue

                node_id = self.attr(nf, ["node_id", "NODE_ID", "id", "Id", "ID", "nome", "Name"])
                if node_id is None:
                    node_id = f"N{nf.id()}"
                node_id = str(node_id)

                out_c = conduits_info["outgoing"].get(node_id, [])
                in_c = conduits_info["incoming"].get(node_id, [])
                ref_c = out_c[0] if out_c else (in_c[0] if in_c else None)

                # Prefer elevations from the node layer; if missing, derive them from conduit elevations.
                q_scorr = self.attr_float(nf, ["q_scorr", "Q_SCORR", "quota_fondo", "invert", "Invert", "invert_elevation", "q_fondo"], None)
                if q_scorr is None:
                    vals = []
                    for c in out_c:
                        if c.get("z_monte") is not None:
                            vals.append(c["z_monte"])
                    for c in in_c:
                        if c.get("z_valle") is not None:
                            vals.append(c["z_valle"])
                    if vals:
                        q_scorr = sum(vals) / len(vals)
                    else:
                        q_scorr = 0.0
                        warnings += 1

                elevaz = self.attr_float(nf, ["elevaz", "ELEVaz", "ELEVAZ", "quota_terr", "Quota_T", "ground_elevation", "ELEVATION", "q_terr"], None)
                prof_scav = self.attr_float(nf, ["prof_scav", "PROF_SCAV", "max_depth", "prof", "excavation_depth", "depth"], None)

                if prof_scav is None and elevaz is not None:
                    prof_scav = max(elevaz - q_scorr, 0.0)
                elif prof_scav is None:
                    prof_scav = 2.0

                if elevaz is None:
                    elevaz = q_scorr + prof_scav

                distanza = ref_c.get("length") if ref_c else 0.0
                pendenza = ref_c.get("slope") if ref_c else None
                materiale = ref_c.get("materiale") if ref_c else ""
                diam_m = ref_c.get("diam_m") if ref_c else None
                estradosso = q_scorr + diam_m if diam_m is not None else None
                id_monte = ref_c.get("from_node") if ref_c else ""
                id_valle = ref_c.get("to_node") if ref_c else ""
                pk = pk_map.get(node_id)
                note = ""
                if len(out_c) > 1:
                    note = f"Nodo con {len(out_c)} condotte uscenti; usata la prima per campi tratta."

                of = QgsFeature(mem.fields())
                of.setGeometry(QgsGeometry(geom))
                of["node_id"] = node_id
                of["elevaz"] = float(elevaz)
                of["q_scorr"] = float(q_scorr)
                of["prof_scav"] = float(prof_scav)
                of["pk"] = float(pk) if pk is not None else None
                of["Distance"] = float(distanza) if distanza is not None else 0.0
                of["pendenza"] = float(pendenza) if pendenza is not None else None
                of["materiale"] = str(materiale) if materiale is not None else ""
                of["diam_m"] = float(diam_m) if diam_m is not None else None
                of["estradosso"] = float(estradosso) if estradosso is not None else None
                of["id_monte"] = str(id_monte) if id_monte is not None else ""
                of["id_valle"] = str(id_valle) if id_valle is not None else ""
                of["note"] = note
                pr.addFeature(of)
                count += 1

            mem.updateExtents()

            driver = "GPKG" if out_path.lower().endswith(".gpkg") else "ESRI Shapefile"
            QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", mem.crs(), driver)

            # Reload the saved layer so the user can immediately use it as the SWMM node layer.
            loaded = QgsVectorLayer(out_path, "manhole_calculated_swmm", "ogr")
            if loaded.isValid():
                QgsProject.instance().addMapLayer(loaded)
                self.refresh_layers()

            self.log_msg(f"File nodi-pozzetti creato: {out_path}")
            self.log_msg(f"Pozzetti esportati: {count}")
            if warnings:
                self.log_msg(f"ATTENZIONE: {warnings} nodi senza q_scorr ricostruibile; impostato q_scorr = 0.0.")

            QMessageBox.information(self, "SWMM Sewer Builder", f"File nodi-pozzetti creato correttamente:\n{out_path}")

        except Exception as e:
            self.log_msg(f"ERRORE creazione pozzetti: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def collect_conduit_info_for_pozzetti(self, cond_layer):
        outgoing = {}
        incoming = {}
        all_conduits = []

        for f in cond_layer.getFeatures():
            geom = f.geometry()
            if not geom or geom.isEmpty():
                continue

            from_node = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
            to_node = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])
            if from_node is None or to_node is None:
                continue
            from_node = str(from_node)
            to_node = str(to_node)

            cond_id = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
            if cond_id is None:
                cond_id = f"COND_{f.id()}"

            length_field = self.attr_float(f, ["Lenght", "LENGHT", "length", "Length", "LENGTH", "lunghezza"], None)
            length = length_field if length_field and length_field > 0 else geom.length()

            diam_mm = self.attr_float(f, ["diam_mm", "DN", "diametro", "D_mm"], None)
            diam_m = self.attr_float(f, ["D", "diam_m"], None)
            if diam_m is None and diam_mm:
                diam_m = diam_mm / 1000.0

            materiale = self.attr(f, ["materiale", 'Material', "MATERIALE", "mat", "MAT"])
            slope = self.attr_float(f, ["Slope", "SLOPE", "pendenza", "Pendenza"], None)
            z_monte = self.attr_float(f, ["z_monte", "Z_MONTE", "scorr_monte", "q_monte"], None)
            z_valle = self.attr_float(f, ["z_valle", "Z_VALLE", "scorr_valle", "q_valle"], None)

            c = {
                "cond_id": str(cond_id),
                "from_node": from_node,
                "to_node": to_node,
                "length": max(float(length), 0.0),
                "diam_m": diam_m,
                "materiale": materiale or "",
                "slope": slope,
                "z_monte": z_monte,
                "z_valle": z_valle,
            }
            outgoing.setdefault(from_node, []).append(c)
            incoming.setdefault(to_node, []).append(c)
            all_conduits.append(c)

        # Sort by ID to keep results stable when multiple pipes are present.
        for d in (outgoing, incoming):
            for k in d:
                d[k].sort(key=lambda x: x.get("cond_id", ""))

        return {"outgoing": outgoing, "incoming": incoming, "all": all_conduits}

    def compute_pk_map(self, conduits_info):
        """Estimate chainage along the network using id_monte/id_valle and Lenght.

        For networks with multiple pipes, chainage is assigned along the shortest path found
        from source nodes. If the network is closed, the first available node is
        used as the starting point with pk = 0.
        """
        outgoing = conduits_info["outgoing"]
        all_c = conduits_info["all"]
        if not all_c:
            return {}

        froms = {c["from_node"] for c in all_c}
        tos = {c["to_node"] for c in all_c}
        starts = sorted(froms - tos)
        if not starts:
            starts = sorted(froms)[:1]

        pk = {}
        queue = []
        for s in starts:
            pk[s] = 0.0
            queue.append(s)

        while queue:
            node = queue.pop(0)
            base = pk.get(node, 0.0)
            for c in outgoing.get(node, []):
                nxt = c["to_node"]
                cand = base + (c.get("length") or 0.0)
                if nxt not in pk or cand < pk[nxt]:
                    pk[nxt] = cand
                    queue.append(nxt)
        return pk


    def prepare_swmm_inputs_dialog(self):
        """Open the layer merge dialog and create the two consolidated SWMM input layers."""
        try:
            point_layers = []
            line_layers = []
            for lyr in QgsProject.instance().mapLayers().values():
                if not isinstance(lyr, QgsVectorLayer):
                    continue
                gt = QgsWkbTypes.geometryType(lyr.wkbType())
                if gt == QgsWkbTypes.PointGeometry:
                    point_layers.append(lyr)
                elif gt == QgsWkbTypes.LineGeometry:
                    line_layers.append(lyr)
            dlg = MergeSwmmInputsDialog(self, point_layers, line_layers, self.txt_outdir.text().strip() or os.path.expanduser("~"))
            if dlg.exec_() != QDialog.Accepted:
                return
            outdir = dlg.output_dir()
            os.makedirs(outdir, exist_ok=True)
            line_ids = dlg.selected_line_ids()
            point_ids = dlg.selected_point_ids()
            if not line_ids:
                raise Exception('Select at least one conduit/link layer to merge.')
            if not point_ids:
                raise Exception('Select at least one manhole/node layer to merge.')
            line_layers_sel = [QgsProject.instance().mapLayer(i) for i in line_ids if QgsProject.instance().mapLayer(i)]
            point_layers_sel = [QgsProject.instance().mapLayer(i) for i in point_ids if QgsProject.instance().mapLayer(i)]
            lines_path = os.path.join(outdir, dlg.output_lines_name())
            points_path = os.path.join(outdir, dlg.output_points_name())
            if not lines_path.lower().endswith(".gpkg"):
                lines_path += ".gpkg"
            if not points_path.lower().endswith(".gpkg"):
                points_path += ".gpkg"
            merged_lines = self._merge_line_layers_for_swmm(line_layers_sel, lines_path)
            merged_points = self._merge_point_layers_for_swmm(point_layers_sel, points_path)
            self.refresh_layers()
            if merged_lines and merged_lines.isValid():
                idx = self.cmb_condotte.findData(merged_lines.id())
                if idx >= 0:
                    self.cmb_condotte.setCurrentIndex(idx)
            if merged_points and merged_points.isValid():
                idx = self.cmb_nodi.findData(merged_points.id())
                if idx >= 0:
                    self.cmb_nodi.setCurrentIndex(idx)
            self.log_msg(f"SWMM inputs prepared: {lines_path} | {points_path}")
            QMessageBox.information(self, "Input SWMM", "Consolidated files created and set as SWMM model inputs:\n\n" + lines_path + "\n" + points_path)
        except Exception as e:
            self.log_msg(f"SWMM input preparation error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def _merge_line_layers_for_swmm(self, layers, out_path):
        if not layers:
            return None
        crs = layers[0].crs()
        fields = QgsFields()
        for name, typ in [
            ("cond_id", QVariant.String), ("id_monte", QVariant.String), ("id_valle", QVariant.String),
            ("pk_monte", QVariant.Double), ("pk_valle", QVariant.Double),
            ("z_monte", QVariant.Double), ("z_valle", QVariant.Double),
            ("length", QVariant.Double), ("Lenght", QVariant.Double),
            ("pendenza", QVariant.Double), ("Slope", QVariant.Double),
            ("materiale", QVariant.String), ("diam_m", QVariant.Double), ("source", QVariant.String)
        ]:
            fields.append(QgsField(name, typ))
        mem = QgsVectorLayer(f"LineString?crs={crs.authid()}", "input_swmm_tratte", "memory")
        pr = mem.dataProvider(); pr.addAttributes(fields); mem.updateFields()
        count = 0
        for lyr in layers:
            tr = None
            if lyr.crs() != crs:
                tr = QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance())
            for f in lyr.getFeatures():
                g = QgsGeometry(f.geometry())
                if not g or g.isEmpty():
                    continue
                if tr:
                    g.transform(tr)
                of = QgsFeature(mem.fields())
                of.setGeometry(g)
                cond = self.attr(f, ["cond_id", "id", "ID"], f"{lyr.name()}_{f.id()}")
                of["cond_id"] = str(cond)
                of["id_monte"] = str(self.attr(f, ["id_monte", "from_node", "FROM_NODE"], ""))
                of["id_valle"] = str(self.attr(f, ["id_valle", "to_node", "TO_NODE"], ""))
                of["pk_monte"] = self.attr_float(f, ["pk_monte"], None)
                of["pk_valle"] = self.attr_float(f, ["pk_valle"], None)
                of["z_monte"] = self.attr_float(f, ["z_monte", "Z_MONTE"], None)
                of["z_valle"] = self.attr_float(f, ["z_valle", "Z_VALLE"], None)
                L = self.attr_float(f, ["Lenght", "length", "LENGTH"], None)
                if L is None:
                    L = float(g.length())
                of["length"] = round(float(L), 3)
                of["Lenght"] = round(float(L), 3)
                sl = self.attr_float(f, ["Slope", "pendenza", "PENDENZA"], None)
                if sl is not None and abs(float(sl)) > 0.5:
                    sl = float(sl) / 100.0
                if sl is None:
                    z_m, z_v = of["z_monte"], of["z_valle"]
                    sl = (float(z_m) - float(z_v)) / float(L) if z_m not in [None, ""] and z_v not in [None, ""] and L else None
                of["pendenza"] = round(float(sl), 5) if sl is not None else None
                of["Slope"] = round(float(sl), 5) if sl is not None else None
                of["materiale"] = str(self.attr(f, ["materiale", "MATERIALE"], ""))
                of["diam_m"] = self.attr_float(f, ["diam_m", "D", "diametro", "diam_mm", "DN"], None)
                dm = of["diam_m"]
                if dm not in [None, ""] and float(dm) > 10:
                    of["diam_m"] = float(dm) / 1000.0
                of["source"] = lyr.name()
                pr.addFeature(of); count += 1
        mem.updateExtents()
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", crs, "GPKG")
        out = QgsVectorLayer(out_path, "input_swmm_tratte", "ogr")
        if out.isValid():
            QgsProject.instance().addMapLayer(out)
        self.log_msg(f"Unite {count} tratte in {out_path}")
        return out

    def _merge_point_layers_for_swmm(self, layers, out_path):
        if not layers:
            return None
        crs = layers[0].crs()
        fields = QgsFields()
        for name, typ in [
            ("node_id", QVariant.String), ("ground_elevation", QVariant.Double), ("elevaz", QVariant.Double),
            ("q_scorr", QVariant.Double), ("invert_elevation", QVariant.Double),
            ("prof_scav", QVariant.Double), ("excavation_depth", QVariant.Double),
            ("pk", QVariant.Double), ("Distance", QVariant.Double),
            ("pendenza", QVariant.Double), ("materiale", QVariant.String), ("diam_m", QVariant.Double),
            ("branch", QVariant.String), ("is_conn", QVariant.Int), ("source", QVariant.String)
        ]:
            fields.append(QgsField(name, typ))
        mem = QgsVectorLayer(f"Point?crs={crs.authid()}", "input_swmm_pozzetti", "memory")
        pr = mem.dataProvider(); pr.addAttributes(fields); mem.updateFields()
        by_id = {}
        order = []
        for lyr in layers:
            tr = None
            if lyr.crs() != crs:
                tr = QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance())
            for f in lyr.getFeatures():
                nid = self.attr(f, ["node_id", "Id", "ID", "id"], None)
                if nid is None or str(nid).strip() == "":
                    nid = f"{lyr.name()}_{f.id()}"
                nid = str(nid).strip()
                g = QgsGeometry(f.geometry())
                if not g or g.isEmpty():
                    continue
                if tr:
                    g.transform(tr)
                of = QgsFeature(mem.fields())
                of.setGeometry(g)
                of["node_id"] = nid
                elev = self.attr_float(f, ["elevaz", "ground_elevation", "quota_terr"], None)
                q = self.attr_float(f, ["q_scorr", "invert_elevation", "quota_fondo", "invert"], None)
                prof = self.attr_float(f, ["prof_scav", "excavation_depth"], None)
                if prof is None and elev is not None and q is not None:
                    prof = float(elev) - float(q)
                of["ground_elevation"] = elev
                of["elevaz"] = elev
                of["q_scorr"] = q
                of["invert_elevation"] = q
                of["prof_scav"] = prof
                of["excavation_depth"] = prof
                of["pk"] = self.attr_float(f, ["pk", "PK"], None)
                of["Distance"] = self.attr_float(f, ["Distance", "distanza"], None)
                sl = self.attr_float(f, ["pendenza", "Slope"], None)
                if sl is not None and abs(float(sl)) > 0.5:
                    sl = float(sl) / 100.0
                of["pendenza"] = sl
                of["materiale"] = str(self.attr(f, ["materiale", "MATERIALE"], ""))
                dm = self.attr_float(f, ["diam_m", "D", "diametro", "diam_mm", "DN"], None)
                if dm is not None and float(dm) > 10:
                    dm = float(dm) / 1000.0
                of["diam_m"] = dm
                of["branch"] = str(self.attr(f, ["branch", "branch_prefix"], ""))
                of["is_conn"] = int(self.attr_float(f, ["is_conn"], 0) or 0)
                of["source"] = lyr.name()
                # Deduplicate by node_id.
                # When an invert drop exists at the same manhole, there may be
                # two rows with the same node_id: a higher conduit inlet elevation
                # and a lower manhole bottom elevation. In SWMM, the node must use
                # the lower value as invert elevation, while the higher value remains
                # on the conduits as z_monte/z_valle and becomes a positive offset.
                if nid not in by_id:
                    by_id[nid] = of; order.append(nid)
                else:
                    old = by_id[nid]
                    old_q = _to_float(old["q_scorr"], None)
                    new_q = _to_float(of["q_scorr"], None)
                    if old_q is None and new_q is not None:
                        by_id[nid] = of
                    elif old_q is not None and new_q is not None and new_q < old_q:
                        by_id[nid] = of
        for nid in order:
            pr.addFeature(by_id[nid])
        mem.updateExtents()
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        QgsVectorFileWriter.writeAsVectorFormat(mem, out_path, "UTF-8", crs, "GPKG")
        out = QgsVectorLayer(out_path, "input_swmm_pozzetti", "ogr")
        if out.isValid():
            QgsProject.instance().addMapLayer(out)
        self.log_msg(f"Uniti {len(order)} pozzetti/nodi in {out_path}")
        return out

    def generate_model(self):
        try:
            cond_layer = self.get_layer(self.cmb_condotte)
            nodi_layer = self.get_layer(self.cmb_nodi)
            if not cond_layer or not nodi_layer:
                raise Exception('Select the conduit layer and the node layer.')
            if self.basin_geom is None:
                raise Exception('Draw the catchment polygon first.')

            outdir = self.txt_outdir.text().strip()
            if not outdir:
                raise Exception('Select una output folder.')
            os.makedirs(outdir, exist_ok=True)

            self.log.clear()
            self.log_msg("Starting model generation...")

            basin_geom = QgsGeometry(self.basin_geom)
            if self.basin_crs and self.basin_crs != nodi_layer.crs():
                tr = QgsCoordinateTransform(self.basin_crs, nodi_layer.crs(), QgsProject.instance())
                basin_geom.transform(tr)

            nodi, nodi_features = self.read_nodes(nodi_layer, basin_geom)
            extra_outfall_defs = self.apply_manual_network_additions(nodi, nodi_features, nodi_layer)
            self.export_manual_nodes_outfalls_to_gpkg(outdir, nodi_layer.crs())
            if len(nodi_features) < 2:
                raise Exception('At least 2 nodes are required inside the catchment/model.')
            self.log_msg(f"Nodi nel modello: {len(nodi_features)}")
            if extra_outfall_defs:
                self.log_msg(f"Outfall manuali aggiunti: {len(extra_outfall_defs)}")

            condotte = self.read_conduits(cond_layer, nodi_layer, nodi)
            manual_condotte = self.get_manual_conduit_definitions(nodi)
            if manual_condotte:
                condotte.extend(manual_condotte)
                self.log_msg(f"Condotte manuali aggiunte: {len(manual_condotte)}")
            n_shape_overrides = self.apply_conduit_shape_overrides(condotte)
            if n_shape_overrides:
                self.log_msg(f"Modifiche shape/dimensioni condotte applicate: {n_shape_overrides}.")
            seen_cond_ids = set()
            for c in condotte:
                cid = str(c.get("cond_id"))
                if cid in seen_cond_ids:
                    raise Exception(f"ID condotta/link duplicato nel modello: {cid}.")
                seen_cond_ids.add(cid)
            self.log_msg(f"Condotte lette: {len(condotte)}")
            if not condotte:
                raise Exception("No valid conduit was read.")

            outfall_node = self.txt_outfall.text().strip()
            if not outfall_node and not extra_outfall_defs:
                raise Exception('Enter at least one outfall: final outfall node or new manual outfall.')
            if outfall_node and outfall_node not in nodi:
                raise Exception(
                    f"Il nodo outfall '{outfall_node}' non è presente tra i nodi interni letti dal layer nodi/manuali. "
                    'Check the node ID field or the catchment boundary.'
                )
            if outfall_node:
                self.log_msg(f"Nodo outfall impostato dall'utente: {outfall_node}")
            outfall_ids = set([outfall_node] if outfall_node else []) | {str(o.get("node_id")) for o in extra_outfall_defs}
            storage_def = self.get_storage_definition(nodi, outfall_ids)
            if storage_def:
                self.log_msg(
                    f"Storage node impostato dall'utente: {storage_def['node_id']} "
                    f"con curva {storage_def['curve_name']} ({len(storage_def['curve'])} punti)."
                )
            orifice_defs = self.get_orifice_definitions(condotte, nodi)
            if orifice_defs:
                self.log_msg(f"Orifizi impostati dall'utente: {len(orifice_defs)}.")
            weir_defs = self.get_weir_definitions(condotte, nodi)
            if weir_defs:
                self.log_msg(f"Weir impostati dall'utente: {len(weir_defs)}.")
            pump_defs = self.get_pump_definitions(condotte, nodi)
            used_replace_links = {}
            for label, items, key in [('orifice', orifice_defs, "link_id"), ("weir", weir_defs, "link_id"), ('pump', pump_defs, "pump_id")]:
                for item in items:
                    repl = str(item.get("replace_link", "") or "")
                    if not repl:
                        continue
                    if repl in used_replace_links:
                        raise Exception(f"La condotta {repl} non può essere trasformata sia in {used_replace_links[repl]} sia in {label}.")
                    used_replace_links[repl] = label
            if pump_defs:
                self.log_msg(f"Pompe impostate dall'utente: {len(pump_defs)}.")

            self.export_added_elements_to_gpkg(
                outdir,
                nodi_layer.crs(),
                nodi,
                storage_def=storage_def,
                orifice_defs=orifice_defs,
                weir_defs=weir_defs,
                pump_defs=pump_defs,
                manual_conduits=manual_condotte,
            )

            subcatchments, sub_layer = self.create_subcatchments(nodi_layer, nodi_features, basin_geom, outfall_ids)
            self.subcatch_layer = sub_layer
            QgsProject.instance().addMapLayer(sub_layer)
            self.log_msg(f"Sottobacini generati: {len(subcatchments)}")

            model_base = "modello_swmm"
            inp_file = os.path.join(outdir, model_base + ".inp")
            rpt_file = os.path.join(outdir, model_base + ".rpt")
            out_file = os.path.join(outdir, model_base + ".out")

            inp_text = self.build_inp(nodi, condotte, subcatchments, basin_geom, outfall_node, storage_def, orifice_defs, pump_defs, weir_defs, extra_outfall_defs)
            with open(inp_file, "w", encoding="utf-8") as f:
                f.write(inp_text)
            self.log_msg(f"File INP creato: {inp_file}")

            # Save subcatchments to GeoPackage.
            gpkg_path = os.path.join(outdir, "sottobacini_swmm.gpkg")
            QgsVectorFileWriter.writeAsVectorFormat(sub_layer, gpkg_path, "UTF-8", sub_layer.crs(), "GPKG")
            self.log_msg(f"Layer sottobacini salvato: {gpkg_path}")

            if self.chk_run.isChecked():
                self.run_swmm_qgis_python(inp_file, rpt_file, out_file)
                self.log_msg(f"Report RPT: {rpt_file}")
                self.log_msg(f"Output OUT: {out_file}")

                if self.chk_load_results.isChecked():
                    self.load_swmm_results_to_project(cond_layer, nodi_layer, rpt_file, outdir, out_file, orifice_defs, pump_defs, weir_defs)

            QMessageBox.information(self, "SWMM Sewer Builder", "SWMM model generated successfully.")

        except Exception as e:
            self.log_msg(f"ERRORE: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def attr(self, feat, names, default=None):
        """Read the first available field from names.
        A default value is also accepted for compatibility with snap utilities.
        """
        available = feat.fields().names()
        for n in names:
            if n in available:
                v = feat[n]
                if v not in (None, ""):
                    return v
        return default

    def attr_float(self, feat, names, default=None):
        v = self.attr(feat, names)
        if v is None:
            return default
        try:
            return float(str(v).replace(",", "."))
        except Exception:
            return default

    def read_nodes(self, nodi_layer, basin_geom):
        nodi = {}
        feats = []
        for f in nodi_layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            if not (basin_geom.contains(g) or basin_geom.intersects(g)):
                continue
            p = g.asPoint()
            node_id = self.attr(f, ["node_id", "NODE_ID", "id", "Id", "ID", "nome", "Name"])
            if node_id is None:
                node_id = f"N{f.id()}"
            quota_terr = self.attr_float(f, ["elevaz", "ELEVaz", "ELEVAZ", "quota_terr", "Quota_T", "ground_elevation", "ELEVATION", "q_terr"], 0.0)
            quota_fondo = self.attr_float(f, ["q_scorr", "Q_SCORR", "quota_fondo", "invert", "Invert", "invert_elevation", "q_fondo"], quota_terr - 2.0)
            max_depth = self.attr_float(f, ["prof_scav", "PROF_SCAV", "max_depth", "prof", "excavation_depth", "depth"], max(0.1, quota_terr - quota_fondo))
            nodi[str(node_id)] = {
                "node_id": str(node_id),
                "fid": f.id(),
                "x": p.x(),
                "y": p.y(),
                "quota_terr": quota_terr,
                "quota_fondo": quota_fondo,
                "max_depth": max_depth,
                "feature": f,
            }
            feats.append(QgsFeature(f))
        return nodi, feats

    def read_conduits(self, cond_layer, nodi_layer, nodi):
        node_index = QgsSpatialIndex()
        fid_to_node = {}
        node_ids_set = set(nodi.keys())
        for n in nodi.values():
            feat = n["feature"]
            node_index.addFeature(feat)
            fid_to_node[feat.id()] = n

        conduits = []
        for f in cond_layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            line = self.first_polyline(g)
            if not line or len(line) < 2:
                continue

            cond_id = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
            if cond_id is None:
                cond_id = f"COND_{f.id()}"
            cond_id = str(cond_id)

            from_node = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
            to_node = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])

            if from_node is None or str(from_node) not in node_ids_set:
                from_node = self.nearest_node_id(node_index, fid_to_node, QgsPointXY(line[0]))
            if to_node is None or str(to_node) not in node_ids_set:
                to_node = self.nearest_node_id(node_index, fid_to_node, QgsPointXY(line[-1]))
            if not from_node or not to_node:
                self.log_msg(f"Condotta {cond_id} saltata: nodi non riconosciuti.")
                continue
            if str(from_node) not in node_ids_set or str(to_node) not in node_ids_set:
                # Use only conduits inside the catchment in this first implementation.
                continue

            diam_mm = self.attr_float(f, ["diam_mm", "DN", "diametro", "D_mm"], None)
            diam_m = self.attr_float(f, ["D", "diam_m"], None)
            if diam_m is None:
                diam_m = (diam_mm / 1000.0) if diam_mm else 0.300

            # Manning roughness n is set by the user in the interface,
            # not read from the shapefile.
            roughness = self.spn_roughness.value()

            length_field = self.attr_float(f, ["Lenght", "LENGHT", "length", "Length", "LENGTH", "lunghezza"], None)
            length = length_field if length_field and length_field > 0 else g.length()

            z_monte = self.attr_float(f, ["z_monte", "Z_MONTE", "scorr_monte", "q_monte"], None)
            z_valle = self.attr_float(f, ["z_valle", "Z_VALLE", "scorr_valle", "q_valle"], None)

            quota_monte_nodo = nodi[str(from_node)]["quota_fondo"]
            quota_valle_nodo = nodi[str(to_node)]["quota_fondo"]

            in_offset = 0.0 if z_monte is None else z_monte - quota_monte_nodo
            out_offset = 0.0 if z_valle is None else z_valle - quota_valle_nodo

            # Negative offsets are not allowed in SWMM. If conduit invert elevations
            # are lower than the node invert elevation, offsets are set to 0
            # and the anomaly is reported in the log.
            if in_offset < 0:
                self.log_msg(f"ATTENZIONE condotta {cond_id}: InOffset negativo ({in_offset:.3f} m). Impostato a 0.")
                in_offset = 0.0
            if out_offset < 0:
                self.log_msg(f"ATTENZIONE condotta {cond_id}: OutOffset negativo ({out_offset:.3f} m). Impostato a 0.")
                out_offset = 0.0

            slope = self.attr_float(f, ["Slope", "SLOPE", "pendenza", "Pendenza"], None)

            conduits.append({
                "cond_id": cond_id,
                "from_node": str(from_node),
                "to_node": str(to_node),
                "length": max(length, 0.1),
                "diam_m": max(diam_m, 0.05),
                "shape": "CIRCULAR",
                "geom1": max(diam_m, 0.05),
                "geom2": 0.0,
                "geom3": 0.0,
                "geom4": 0.0,
                "roughness": roughness,
                "z_monte": z_monte,
                "z_valle": z_valle,
                "in_offset": in_offset,
                "out_offset": out_offset,
                "slope": slope,
                # Actual vertices of the conduit geometry. They are written
                # to the [VERTICES] section of the INP file to make the model
                # more compatible with viewers/plugins that rebuild the map.
                "vertices": [(QgsPointXY(p).x(), QgsPointXY(p).y()) for p in line],
            })
        return conduits

    def first_polyline(self, geom):
        if geom.isMultipart():
            m = geom.asMultiPolyline()
            return m[0] if m else None
        return geom.asPolyline()

    def nearest_node_id(self, index, fid_to_node, point):
        ids = index.nearestNeighbor(point, 1)
        if not ids:
            return None
        return fid_to_node[ids[0]]["node_id"]

    def run_processing_first_available(self, alg_ids, params, feedback=None):
        """Run the first available Processing algorithm among the provided IDs.

        This improves compatibility with portable QGIS installations and different
        QGIS versions, where the same algorithm may be registered with different
        IDs, such as native:... or qgis:...
        """
        last_error = None
        for alg_id in alg_ids:
            try:
                return processing.run(alg_id, params, feedback=feedback)
            except Exception as e:
                last_error = e
                continue

        raise Exception(
            "None of the required Processing algorithms is available: "
            + ", ".join(alg_ids)
            + ". Check that the QGIS 'Processing' plugin is enabled. "
            + f"Ultimo errore: {last_error}"
        )


    def _make_valid_polygon_geometry(self, geom, label="geometria"):
        """Return a valid copy of the polygon geometry, when possible.

        This is mainly required for manually drawn catchments: if the polygon has
        self-intersections or minor topology issues, Processing algorithms may
        fail. The function first tries makeValid() and then the standard buffer(0)
        fallback.
        """
        if geom is None:
            return QgsGeometry()
        try:
            g = QgsGeometry(geom)
        except Exception:
            g = geom
        if g is None or g.isEmpty():
            return QgsGeometry()

        try:
            if hasattr(g, "isGeosValid") and not g.isGeosValid():
                if hasattr(g, "makeValid"):
                    gv = g.makeValid()
                    if gv is not None and not gv.isEmpty():
                        g = gv
                try:
                    if hasattr(g, "isGeosValid") and not g.isGeosValid():
                        gb = g.buffer(0, 8)
                        if gb is not None and not gb.isEmpty():
                            g = gb
                except Exception:
                    pass
        except Exception:
            try:
                gb = g.buffer(0, 8)
                if gb is not None and not gb.isEmpty():
                    g = gb
            except Exception:
                pass
        return g

    def _manual_clip_polygons(self, input_layer, overlay_geom, crs_auth, out_name="clip_interno_swmm"):
        """Perform an internal geometry clip without using Processing.

        This avoids failures when native:clip/qgis:clip are not available or when
        Processing rejects invalid geometries. The function returns a memory layer
        containing the input fields and geometries intersected with the catchment.
        """
        out_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_auth}", out_name, "memory")
        pr = out_layer.dataProvider()
        pr.addAttributes(input_layer.fields())
        out_layer.updateFields()

        ov = self._make_valid_polygon_geometry(overlay_geom, "bacino")
        if ov is None or ov.isEmpty() or ov.area() <= 0:
            raise Exception('The catchment polygon is invalid or has zero area.')

        feats = []
        for f in input_layer.getFeatures():
            try:
                fg = self._make_valid_polygon_geometry(f.geometry(), "Voronoi polygon")
                if fg is None or fg.isEmpty():
                    continue
                if not fg.intersects(ov):
                    continue
                inter = fg.intersection(ov)
                inter = self._make_valid_polygon_geometry(inter, "intersezione")
                if inter is None or inter.isEmpty() or inter.area() <= 0:
                    continue
                nf = QgsFeature(out_layer.fields())
                nf.setAttributes(f.attributes())
                nf.setGeometry(inter)
                feats.append(nf)
            except Exception:
                continue
        if feats:
            pr.addFeatures(feats)
        out_layer.updateExtents()
        return out_layer

    def geometry_polygon_parts(self, geom):
        """Return a list of single Polygon geometries from a Polygon or MultiPolygon."""
        if geom is None or geom.isEmpty():
            return []
        if not geom.isMultipart():
            return [QgsGeometry(geom)]

        parts = []
        try:
            for poly in geom.asMultiPolygon():
                if poly:
                    g = QgsGeometry.fromPolygonXY(poly)
                    if g and not g.isEmpty() and g.area() > 0:
                        parts.append(g)
        except Exception:
            try:
                for g0 in geom.asGeometryCollection():
                    g = QgsGeometry(g0)
                    if g and not g.isEmpty() and g.area() > 0:
                        parts.append(g)
            except Exception:
                pass
        return parts

    def sample_dtm_value(self, raster_layer, point_xy, source_crs=None):
        """Sample the DTM at a point.

        point_xy must be expressed in source_crs. If the raster uses a different
        CRS, the point is transformed to the raster CRS before identify() is called.
        Returns a float value or None.
        """
        if raster_layer is None:
            return None
        try:
            sample_pt = QgsPointXY(point_xy)
            if source_crs is not None and raster_layer.crs().isValid() and source_crs.isValid() and source_crs != raster_layer.crs():
                tr = QgsCoordinateTransform(source_crs, raster_layer.crs(), QgsProject.instance())
                sample_pt = tr.transform(sample_pt)
            ident = raster_layer.dataProvider().identify(sample_pt, QgsRaster.IdentifyFormatValue)
            if not ident.isValid():
                return None
            vals = list(ident.results().values())
            if not vals:
                return None
            val = vals[0]
            if val is None:
                return None
            try:
                fval = float(val)
                if math.isnan(fval):
                    return None
                return fval
            except Exception:
                return None
        except Exception:
            return None

    def subcatchment_slope_from_dtm(self, polygon_geom, node_feat, node_point_xy, avg_flow_len, default_slope, raster_layer, source_crs):
        """Estimate the slope of a subcatchment from the DTM.

        Samples used: polygon vertices plus centroid/internal point.
        Main formula:
            slope = (mean_sample_elevation - manhole_ground_elevation) / mean_flow_length
        If the elevation drop toward the manhole is not positive, the sampled
        elevation range is used as a fallback:
            slope = (max_elevation - min_elevation) / mean_flow_length
        The value is returned in m/m and constrained to a useful SWMM range.
        """
        try:
            if raster_layer is None or not avg_flow_len or avg_flow_len <= 0:
                return float(default_slope), "manuale_fallback"

            samples = []
            try:
                # Subcatchment vertices.
                for v in polygon_geom.vertices():
                    z = self.sample_dtm_value(raster_layer, QgsPointXY(v.x(), v.y()), source_crs)
                    if z is not None:
                        samples.append(z)
            except Exception:
                pass

            # Representative internal point/centroid: use pointOnSurface to remain inside the polygon.
            try:
                cpt = polygon_geom.pointOnSurface().asPoint()
                zc = self.sample_dtm_value(raster_layer, QgsPointXY(cpt), source_crs)
                if zc is not None:
                    samples.append(zc)
            except Exception:
                try:
                    cpt = polygon_geom.centroid().asPoint()
                    zc = self.sample_dtm_value(raster_layer, QgsPointXY(cpt), source_crs)
                    if zc is not None:
                        samples.append(zc)
                except Exception:
                    pass

            if not samples:
                return float(default_slope), "manuale_fallback"

            # Manhole ground elevation: prefer the attribute field; if missing, sample the DTM at the node.
            z_node = self.attr_float(node_feat, ["elevaz", "ground_elevation", "quota_terr", "Quota_T", "ELEVATION"], None)
            if z_node is None:
                z_node = self.sample_dtm_value(raster_layer, node_point_xy, source_crs)

            z_mean = sum(samples) / len(samples)
            z_min = min(samples)
            z_max = max(samples)

            if z_node is not None:
                dz = z_mean - float(z_node)
            else:
                dz = z_max - z_min

            # If the node is not lower than the sampled mean elevation, still use
            # the internal elevation variability of the subcatchment.
            if dz <= 0:
                dz = z_max - z_min

            if dz <= 0:
                return float(default_slope), "manuale_fallback"

            slope = float(dz) / float(avg_flow_len)
            # Avoid null/anomalous values; SWMM requires a positive slope.
            slope = max(0.0001, min(slope, 1.0))
            return slope, "DTM"
        except Exception:
            return float(default_slope), "manuale_fallback"


    def _prepare_imperv_surface_config(self, source_crs):
        """Prepare polygon layers used for weighted imperviousness calculation.

        The computation is intentionally lightweight: each subcatchment is
        intersected only with the roof and road layers selected by the user, using
        a spatial index to reduce the candidate features.
        """
        surfaces = []
        if not hasattr(self, "cmb_imperv_mode") or self.cmb_imperv_mode.currentText() != 'Compute from roofs/roads':
            return surfaces

        def _add_surface(combo_name, coeff_widget_name, label):
            combo = getattr(self, combo_name, None)
            coeff_widget = getattr(self, coeff_widget_name, None)
            if combo is None or coeff_widget is None:
                return
            layer_id = combo.currentData()
            if not layer_id:
                return
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is None or not isinstance(layer, QgsVectorLayer):
                return
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
                return
            try:
                index = QgsSpatialIndex(layer.getFeatures())
            except Exception:
                index = None
            surfaces.append({
                "layer": layer,
                "index": index,
                "coeff": float(coeff_widget.value()),
                "label": label,
                "crs": layer.crs(),
            })

        _add_surface("cmb_imperv_roofs", "spn_coeff_roofs", "tetti")
        _add_surface("cmb_imperv_roads", "spn_coeff_roads", "strade")

        if not surfaces:
            self.log_msg('Imperviousness calculation from roofs/roads was requested, but no valid polygon layers were selected: check the selection; if the method is active but there are no intersections, the minimum value of 10% will be applied.')
        return surfaces

    def _intersection_area_with_surface(self, sub_geom, source_crs, surface):
        """Return the intersection area between a subcatchment and a surface layer."""
        try:
            layer = surface.get("layer")
            if layer is None:
                return 0.0

            geom = QgsGeometry(sub_geom)
            if source_crs is not None and layer.crs().isValid() and source_crs.isValid() and source_crs != layer.crs():
                tr = QgsCoordinateTransform(source_crs, layer.crs(), QgsProject.instance())
                geom.transform(tr)

            if geom is None or geom.isEmpty():
                return 0.0

            bbox = geom.boundingBox()
            index = surface.get("index")
            if index is not None:
                candidate_ids = index.intersects(bbox)
                req = QgsFeatureRequest().setFilterFids(candidate_ids) if candidate_ids else QgsFeatureRequest().setFilterFids([])
            else:
                req = QgsFeatureRequest().setFilterRect(bbox)

            area = 0.0
            for sf in layer.getFeatures(req):
                sg = sf.geometry()
                if sg is None or sg.isEmpty():
                    continue
                if not geom.intersects(sg):
                    continue
                try:
                    inter = geom.intersection(sg)
                    if inter and not inter.isEmpty():
                        area += max(0.0, inter.area())
                except Exception:
                    continue
            return area
        except Exception:
            return 0.0

    def impervious_from_surfaces(self, sub_geom, area_mq, source_crs, default_imperv, surfaces):
        """Compute percent imperviousness from a weighted average of impervious surfaces.

        Formula:
            %Imperv = ((A_roofs*C_roofs + A_roads*C_roads) / A_subcatchment) * 100
        where the areas are those falling within the subcatchment.
        """
        # If the method is manual or no valid area is available, use the manual value.
        # If the user selected roofs/roads and the layers are available, the value
        # final value must always be the one computed by the formula, even when it is 0.
        # This way, the manual value no longer acts as a maximum/minimum/fallback
        # when the geometric calculation is actually active.
        if not surfaces or area_mq is None or area_mq <= 0:
            return float(default_imperv), "manuale", 0.0, 0.0, None

        weighted = 0.0
        area_roofs = 0.0
        area_roads = 0.0

        for surface in surfaces:
            a = self._intersection_area_with_surface(sub_geom, source_crs, surface)
            coeff = max(0.0, min(float(surface.get("coeff", 0.0)), 1.0))
            weighted += a * coeff
            if surface.get("label") == "tetti":
                area_roofs += a
            elif surface.get("label") == "strade":
                area_roads += a

        imperv_calc = (weighted / float(area_mq)) * 100.0 if area_mq and area_mq > 0 else 0.0
        imperv_calc = max(0.0, min(imperv_calc, 100.0))

        # If the roof/road calculation is active but there are no intersections,
        # still assign the minimum 10% value requested by the user,
        # avoiding fallback to the manual value.
        if weighted <= 0.0:
            return 10.0, "tetti_strade_min10", area_roofs, area_roads, imperv_calc

        # Even with very small intersections, enforce the minimum 10%
        # when the roof/road method is active.
        imperv_final = max(10.0, imperv_calc)
        return imperv_final, "tetti_strade", area_roofs, area_roads, imperv_calc

    def average_vertex_distance_to_point(self, polygon_geom, point_xy):
        """Compute the mean distance between subcatchment vertices and the manhole.

        This distance represents the mean flow length used to estimate SWMM Width
        as: Width = Area / mean_distance.
        """
        distances = []

        try:
            for vertex in polygon_geom.vertices():
                dx = vertex.x() - point_xy.x()
                dy = vertex.y() - point_xy.y()
                d = math.sqrt(dx * dx + dy * dy)
                if d > 0:
                    distances.append(d)
        except Exception:
            return None

        if not distances:
            return None

        return sum(distances) / len(distances)

    def create_subcatchments(self, nodi_layer, nodi_features, basin_geom, outfall_node=None):
        crs_auth = nodi_layer.crs().authid()
        pts_layer = QgsVectorLayer(f"Point?crs={crs_auth}", "nodi_bacino_swmm", "memory")
        pr = pts_layer.dataProvider()
        pr.addAttributes(nodi_layer.fields())
        pts_layer.updateFields()
        pr.addFeatures(nodi_features)
        pts_layer.updateExtents()

        basin_geom = self._make_valid_polygon_geometry(basin_geom, "bacino")
        if basin_geom is None or basin_geom.isEmpty() or basin_geom.area() <= 0:
            raise Exception(
                'The catchment polygon is invalid or has zero area. '
                'Redraw the catchment avoiding self-intersections, or fix the geometry.'
            )

        basin_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_auth}", "bacino_swmm", "memory")
        bpr = basin_layer.dataProvider()
        bpr.addAttributes([QgsField("id", QVariant.Int)])
        basin_layer.updateFields()
        bf = QgsFeature(basin_layer.fields())
        bf["id"] = 1
        bf.setGeometry(basin_geom)
        bpr.addFeature(bf)
        basin_layer.updateExtents()

        feedback = QgsProcessingFeedback()

        # Compatibility across QGIS versions:
        # in some installations, the Voronoi algorithm is registered as
        # native:voronoipolygons, in altre come qgis:voronoipolygons.
        vor = self.run_processing_first_available(
            ["native:voronoipolygons", "qgis:voronoipolygons"],
            {
                "INPUT": pts_layer,
                "BUFFER": 100.0,
                "OUTPUT": "memory:"
            },
            feedback
        )["OUTPUT"]

        try:
            clipped = self.run_processing_first_available(
                ["native:clip", "qgis:clip"],
                {
                    "INPUT": vor,
                    "OVERLAY": basin_layer,
                    "OUTPUT": "memory:"
                },
                feedback
            )["OUTPUT"]
        except Exception as e:
            # In some QGIS installations, Processing clip fails if the catchment
            # is even slightly invalid or if Processing is not active.
            # To avoid blocking model generation, use an internal clip
            # basato su QgsGeometry.intersection().
            self.log_msg(
                "Subcatchment clipping with Processing failed: using the internal geometry clip. "
                f"Dettaglio: {e}"
            )
            clipped = self._manual_clip_polygons(vor, basin_geom, crs_auth, "sottobacini_clip_interno")

        fields = QgsFields()
        for name, typ in [
            ("sub_id", QVariant.String), ("node_id", QVariant.String), ("outlet", QVariant.String),
            ("area_mq", QVariant.Double), ("area_ha", QVariant.Double), ("imperv", QVariant.Double),
            ("imperv_src", QVariant.String), ("imperv_calc", QVariant.Double), ("area_tetti", QVariant.Double), ("area_strad", QVariant.Double),
            ("width", QVariant.Double), ("slope", QVariant.Double), ("slope_src", QVariant.String), ("curb_len", QVariant.Double)
        ]:
            fields.append(QgsField(name, typ))

        out_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_auth}", "sottobacini_swmm", "memory")
        opr = out_layer.dataProvider()
        opr.addAttributes(fields)
        out_layer.updateFields()

        node_index = QgsSpatialIndex()
        fid_to_feat = {}
        for f in nodi_features:
            node_index.addFeature(f)
            fid_to_feat[f.id()] = f

        subcatchments = []
        imperv = self.spn_imperv.value()
        imperv_surfaces = self._prepare_imperv_surface_config(nodi_layer.crs())
        default_slope = self.spn_slope.value()
        use_dtm_slope = bool(getattr(self, "chk_slope_dtm", None) and self.chk_slope_dtm.isChecked())
        dtm_layer = None
        if use_dtm_slope and hasattr(self, "cmb_dtm"):
            try:
                dtm_layer = QgsProject.instance().mapLayer(self.cmb_dtm.currentData())
            except Exception:
                dtm_layer = None
            if dtm_layer is None:
                self.log_msg('Subcatchment slope from DEM was requested, but no valid DEM was selected: the manual slope will be used as fallback.')

        used_sub_ids = {}

        for poly in clipped.getFeatures():
            for geom in self.geometry_polygon_parts(poly.geometry()):
                if not geom or geom.isEmpty() or geom.area() <= 0:
                    continue

                p = geom.pointOnSurface().asPoint()
                ids = node_index.nearestNeighbor(QgsPointXY(p), 1)
                if not ids:
                    continue

                nf = fid_to_feat[ids[0]]
                node_id = self.attr(nf, ["node_id", "NODE_ID", "id", "Id", "ID", "nome", "Name"])
                if node_id is None:
                    node_id = f"N{nf.id()}"
                node_id = str(node_id)

                # The user-selected outfall represents the final discharge node:
                # it must not have an associated subcatchment, so it is excluded from both
                # both from the subcatchment layer and from the [SUBCATCHMENTS]/[POLYGONS] sections
                # the INP file.
                if outfall_node is not None:
                    outfall_set = set(outfall_node) if isinstance(outfall_node, (set, list, tuple)) else {str(outfall_node)}
                    if node_id in outfall_set:
                        continue

                area_mq = geom.area()
                area_ha = area_mq / 10000.0

                # SWMM hydraulic width: area divided by mean flow length.
                # Mean flow length is estimated as the mean of the
                # distances between subcatchment vertices and the manhole/outlet.
                # For anomalous geometries, a conservative fallback is used.
                node_point = nf.geometry().asPoint()
                avg_flow_len = self.average_vertex_distance_to_point(geom, QgsPointXY(node_point))
                if avg_flow_len and avg_flow_len > 0:
                    width = max(area_mq / avg_flow_len, 1.0)
                else:
                    width = max(math.sqrt(area_mq), 1.0)

                base_sub_id = f"S_{node_id}"
                used_sub_ids[base_sub_id] = used_sub_ids.get(base_sub_id, 0) + 1
                sub_id = base_sub_id if used_sub_ids[base_sub_id] == 1 else f"{base_sub_id}_{used_sub_ids[base_sub_id]}"

                of = QgsFeature(out_layer.fields())
                of.setGeometry(geom)
                of["sub_id"] = sub_id
                of["node_id"] = node_id
                of["outlet"] = node_id
                imperv_value, imperv_src, area_tetti, area_strade, imperv_calc = self.impervious_from_surfaces(
                    geom, area_mq, nodi_layer.crs(), imperv, imperv_surfaces
                )

                of["area_mq"] = area_mq
                of["area_ha"] = area_ha
                of["imperv"] = imperv_value
                if "imperv_src" in [fld.name() for fld in out_layer.fields()]:
                    of["imperv_src"] = imperv_src
                if "imperv_calc" in [fld.name() for fld in out_layer.fields()]:
                    of["imperv_calc"] = imperv_calc if imperv_calc is not None else imperv_value
                if "area_tetti" in [fld.name() for fld in out_layer.fields()]:
                    of["area_tetti"] = area_tetti
                if "area_strad" in [fld.name() for fld in out_layer.fields()]:
                    of["area_strad"] = area_strade
                of["width"] = width
                if use_dtm_slope and dtm_layer is not None:
                    slope_value, slope_src = self.subcatchment_slope_from_dtm(
                        geom, nf, QgsPointXY(node_point), avg_flow_len, default_slope, dtm_layer, nodi_layer.crs()
                    )
                else:
                    slope_value, slope_src = default_slope, "manuale"

                of["slope"] = slope_value
                if "slope_src" in [fld.name() for fld in out_layer.fields()]:
                    of["slope_src"] = slope_src
                of["curb_len"] = 0.0
                opr.addFeature(of)

                subcatchments.append({
                    "sub_id": sub_id, "node_id": node_id, "outlet": node_id,
                    "area_mq": area_mq, "area_ha": area_ha, "imperv": imperv_value,
                    "imperv_src": imperv_src, "imperv_calc": imperv_calc if imperv_calc is not None else imperv_value, "area_tetti": area_tetti, "area_strade": area_strade,
                    "width": width, "slope": slope_value, "slope_src": slope_src, "geometry": geom
                })

        out_layer.updateExtents()
        return subcatchments, out_layer

    def terminal_nodes(self, conduits):
        froms = {c["from_node"] for c in conduits}
        tos = {c["to_node"] for c in conduits}
        terms = sorted(tos - froms)
        return terms if terms else sorted(tos)[-1:]

    def _format_swmm_time(self, minutes):
        minutes = int(round(minutes))
        h = minutes // 60
        m = minutes % 60
        return f"{h:02d}:{m:02d}"

    def _rain_timeseries(self):
        """
        Return a list of (minute, intensity_mm_h) tuples for TS_PIOGGIA.
        Supported hyetograph types:
        - Rectangular: constant mean intensity computed from the IDF curve h=a*t^n.
        - Chicago Keifer-Chu from the IDF curve h = a * t^n, with t in hours and h in mm.

        Continuous Chicago formulation, consistent with the equations reported
        in the referenced document:

            i(theta) = n * a * theta^(n-1)

        with the time origin at the peak. For an event duration D and peak at
        theta_p = r * D:

            before the peak: i_b(theta_b) = n * a * (theta_b / r)^(n-1)
            after  the peak: i_a(theta_a) = n * a * (theta_a / (1-r))^(n-1)

        The plugin discretizes the curve by analytically integrating these two
        functions over each time interval, then computes the mean block intensity
        as i = Delta_h / Delta_t.
        """
        rain_type = self.cmb_rain_type.currentText() if hasattr(self, "cmb_rain_type") else "Rettangolare"
        duration_min = max(1, int(round(self.spn_duration.value())))
        step_min = max(1, int(round(self.spn_rain_step.value()))) if hasattr(self, "spn_rain_step") else 5

        a = float(self.spn_ch_a.value()) if hasattr(self, "spn_ch_a") else float(self.spn_rain.value())
        n = float(self.spn_ch_n.value()) if hasattr(self, "spn_ch_n") else 1.0

        if not str(rain_type).lower().startswith("chicago"):
            # Rectangular rainfall from IDF curve:
            #   h = a * D^n     with D in hours and h in mm
            #   i = h / D       in mm/h
            duration_h = max(duration_min / 60.0, 1e-9)
            total_depth_mm = a * (duration_h ** n)
            rain_intensity = max(0.0, total_depth_mm / duration_h)

            # Always set the first time step to zero so SWMM starts the simulation without rainfall.
            series = [(0, 0.0)]
            t = step_min
            while t < duration_min:
                series.append((t, rain_intensity))
                t += step_min
            series.append((duration_min, rain_intensity))
            series.append((duration_min + step_min, 0.0))
            return series

        r = float(self.spn_ch_r.value())
        r = min(max(r, 0.05), 0.95)
        peak_min = duration_min * r

        def _pow_pos(x, exponent):
            x = max(0.0, float(x))
            if x <= 0.0:
                return 0.0
            return x ** exponent

        def depth_before_peak(t0_min, t1_min):
            """
            Rainfall depth [mm] over interval [t0, t1] before the peak.
            theta_b is measured from the peak toward the left.

            Integral of:
                i_b(theta_b) = n*a*(theta_b/r)^(n-1)

            with theta_b expressed in hours.
            """
            theta_b0_h = max(0.0, (peak_min - float(t0_min)) / 60.0)
            theta_b1_h = max(0.0, (peak_min - float(t1_min)) / 60.0)
            if theta_b0_h <= theta_b1_h:
                return 0.0
            # ∫ n*a*(theta/r)^(n-1) dtheta = a*r^(1-n)*theta^n
            coeff = a * (r ** (1.0 - n))
            return max(0.0, coeff * (_pow_pos(theta_b0_h, n) - _pow_pos(theta_b1_h, n)))

        def depth_after_peak(t0_min, t1_min):
            """
            Rainfall depth [mm] over interval [t0, t1] after the peak.
            theta_a is measured from the peak toward the right.

            Integral of:
                i_a(theta_a) = n*a*(theta_a/(1-r))^(n-1)
            """
            theta_a0_h = max(0.0, (float(t0_min) - peak_min) / 60.0)
            theta_a1_h = max(0.0, (float(t1_min) - peak_min) / 60.0)
            if theta_a1_h <= theta_a0_h:
                return 0.0
            rr = 1.0 - r
            # ∫ n*a*(theta/(1-r))^(n-1) dtheta = a*(1-r)^(1-n)*theta^n
            coeff = a * (rr ** (1.0 - n))
            return max(0.0, coeff * (_pow_pos(theta_a1_h, n) - _pow_pos(theta_a0_h, n)))

        def depth_between(t0_min, t1_min):
            """
            Rainfall depth [mm] over the time block [t0, t1].
            If the block crosses the peak, it is split into two parts.
            """
            t0_min = max(0.0, float(t0_min))
            t1_min = min(float(duration_min), float(t1_min))
            if t1_min <= t0_min:
                return 0.0

            if t1_min <= peak_min:
                return depth_before_peak(t0_min, t1_min)

            if t0_min >= peak_min:
                return depth_after_peak(t0_min, t1_min)

            return depth_before_peak(t0_min, peak_min) + depth_after_peak(peak_min, t1_min)

        # Always set the first time step to zero so SWMM starts the simulation without rainfall.
        # The value computed over block [0, step] is assigned to time step.
        series = [(0, 0.0)]
        t = 0
        while t < duration_min:
            t_next = min(duration_min, t + step_min)
            delta_h = depth_between(t, t_next)
            delta_t_h = max((t_next - t) / 60.0, 1e-9)
            intensity = max(0.0, delta_h / delta_t_h)
            series.append((t_next, intensity))
            t += step_min

        # Zero value after the end of the event.
        series.append((duration_min + step_min, 0.0))
        return series

    def show_rain_graph(self):
        """Display the hyetograph that will be written to the INP file."""
        try:
            series = self._rain_timeseries()
            if not series:
                QMessageBox.warning(self, "Rainfall", 'No storm time series was generated.')
                return

            dlg = QDialog(self)
            dlg.setWindowTitle("Hyetograph chart - TS_PIOGGIA")
            dlg.resize(850, 560)
            lay = QVBoxLayout(dlg)

            rain_type = self.cmb_rain_type.currentText() if hasattr(self, "cmb_rain_type") else "Rettangolare"
            step_min = max(1, int(round(self.spn_rain_step.value()))) if hasattr(self, "spn_rain_step") else 5
            durata_min = max(1, int(round(self.spn_duration.value()))) if hasattr(self, "spn_duration") else 60

            info = QLabel(
                f"Tipo pioggia: <b>{rain_type}</b> &nbsp; | &nbsp; "
                f"Durata evento: <b>{durata_min} min</b> &nbsp; | &nbsp; "
                f"Intervallo temporale: <b>{step_min} min</b>"
            )
            info.setWordWrap(True)
            lay.addWidget(info)

            # Integrated matplotlib plot. If matplotlib is not available, still display the table.
            try:
                from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
                from matplotlib.figure import Figure

                fig = Figure(figsize=(8, 4), dpi=100)
                ax = fig.add_subplot(111)
                x = [float(t) for t, _v in series]
                y = [float(v) for _t, v in series]
                ax.plot(x, y, marker="o", linewidth=1.8)
                ax.fill_between(x, y, step="pre", alpha=0.20)
                ax.set_title("Hyetograph generated - TS_PIOGGIA")
                ax.set_xlabel("Tempo [min]")
                ax.set_ylabel("Intensity [mm/h]")
                ax.grid(True)
                fig.tight_layout()
                canvas = FigureCanvas(fig)
                lay.addWidget(canvas)
            except Exception as e:
                warn = QLabel(
                    'Matplotlib is not available or an error occurred while creating the chart. '
                    "The value table will be displayed.\n" + str(e)
                )
                warn.setWordWrap(True)
                lay.addWidget(warn)

            table = QTableWidget()
            table.setColumnCount(3)
            table.setHorizontalHeaderLabels(["Tempo [min]", "Tempo SWMM", "Intensity [mm/h]"])
            table.setRowCount(len(series))
            for r, (minute, intensity) in enumerate(series):
                table.setItem(r, 0, QTableWidgetItem(str(int(round(minute)))))
                table.setItem(r, 1, QTableWidgetItem(self._format_swmm_time(minute)))
                table.setItem(r, 2, QTableWidgetItem(f"{float(intensity):.3f}"))
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            lay.addWidget(table)

            btns = QDialogButtonBox(QDialogButtonBox.Close)
            btns.rejected.connect(dlg.reject)
            lay.addWidget(btns)
            dlg.exec_()

        except Exception as e:
            QMessageBox.critical(self, 'Error chart storm', str(e))

    def build_inp(self, nodi, conduits, subs, basin_geom, outfall_node, storage_def=None, orifice_defs=None, pump_defs=None, weir_defs=None, extra_outfall_defs=None):
        now = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        terminal = {str(outfall_node)} if outfall_node else set()
        extra_outfall_defs = extra_outfall_defs or []
        extra_outfall_by_id = {str(o.get("node_id")): o for o in extra_outfall_defs}
        terminal.update(extra_outfall_by_id.keys())
        storage_nodes = {str(storage_def["node_id"])} if storage_def else set()
        orifice_defs = orifice_defs or []
        weir_defs = weir_defs or []
        pump_defs = pump_defs or []
        orifice_replace_links = {str(o.get("replace_link")) for o in orifice_defs if o.get("replace_link")}
        weir_replace_links = {str(w.get("replace_link")) for w in weir_defs if w.get("replace_link")}
        pump_replace_links = {str(p.get("replace_link")) for p in pump_defs if p.get("replace_link")}
        regulator_replace_links = orifice_replace_links | weir_replace_links | pump_replace_links
        rain_series = self._rain_timeseries()
        max_rain_min = max([t for t, _v in rain_series], default=int(self.spn_duration.value()))
        end_hours = max(2, math.ceil((max_rain_min + 60) / 60))

        lines = []
        a = lines.append
        a("[TITLE]")
        a(f"; Modello generato automaticamente da SWMM Sewer Builder - {now}")
        a("")
        a("[OPTIONS]")
        opts = [
            ("FLOW_UNITS", "LPS"), ("INFILTRATION", "HORTON"), ("FLOW_ROUTING", "DYNWAVE"),
            ("LINK_OFFSETS", "DEPTH"), ("MIN_SLOPE", "0"), ("ALLOW_PONDING", "NO"),
            ("START_DATE", "01/01/2026"), ("START_TIME", "00:00:00"),
            ("REPORT_START_DATE", "01/01/2026"), ("REPORT_START_TIME", "00:00:00"),
            ("END_DATE", "01/01/2026"), ("END_TIME", f"{end_hours:02d}:00:00"),
            ("REPORT_STEP", "00:01:00"), ("WET_STEP", "00:05:00"), ("DRY_STEP", "01:00:00"),
            ("ROUTING_STEP", "00:00:30"), ("INERTIAL_DAMPING", "PARTIAL"),
            ("NORMAL_FLOW_LIMITED", "BOTH"), ("VARIABLE_STEP", "0.75"),
            ("MAX_TRIALS", "8"), ("HEAD_TOLERANCE", "0.0015"), ("THREADS", "1")
        ]
        for k, v in opts:
            a(f"{k:<22} {v}")
        a("")

        a("[RAINGAGES]")
        a(";;Name           Format    Interval    SCF    Source")
        a(f"RG1              INTENSITY {self._format_swmm_time(max(1, int(round(self.spn_rain_step.value())))):<10} 1.0    TIMESERIES TS_PIOGGIA")
        a("")

        a("[SUBCATCHMENTS]")
        a(";;Name           Raingage        Outlet          Area       %Imperv    Width      Slope      CurbLen    SnowPack")
        for s in subs:
            slope_percent = s["slope"] * 100.0
            a(f"{s['sub_id']:<16} {'RG1':<15} {s['outlet']:<15} {s['area_ha']:<10.4f} {s['imperv']:<10.2f} {s['width']:<10.2f} {slope_percent:<10.3f} {0:<10.2f}")
        a("")

        a("[SUBAREAS]")
        a(";;Subcatchment   N-Imperv    N-Perv      S-Imperv    S-Perv      PctZero    RouteTo    PctRouted")
        for s in subs:
            a(f"{s['sub_id']:<16} {0.015:<11.3f} {0.100:<11.3f} {1.0:<11.3f} {5.0:<11.3f} {25:<10.2f} {'OUTLET':<10} {100:<10.2f}")
        a("")

        a("[INFILTRATION]")
        a(";;Subcatchment   Param1     Param2     Param3     Param4     Param5")
        a(";;-------------- ---------- ---------- ---------- ---------- ----------")
        for s in subs:
            a(f"{s['sub_id']:<16} {99999999.0:<10.1f} {99999999.0:<10.1f} {0.0001:<10.4f} {0.0:<10.1f} {'NaN':<10} HORTON")
        a("")

        a("[JUNCTIONS]")
        a(";;Name           Elevation     MaxDepth     InitDepth    SurDepth     Aponded")
        for node_id, n in nodi.items():
            if node_id in terminal or node_id in storage_nodes:
                continue
            a(f"{node_id:<16} {n['quota_fondo']:<12.3f} {n['max_depth']:<12.3f} {0:<12.3f} {0:<12.3f} {0:<12.3f}")
        a("")

        a("[OUTFALLS]")
        a(";;Name           Elevation     Type       Stage Data       Gated    Route To")
        fixed_stage_raw = ""
        if hasattr(self, "txt_outfall_fixed_stage"):
            fixed_stage_raw = self.txt_outfall_fixed_stage.text().strip().replace(",", ".")
        fixed_stage = _to_float(fixed_stage_raw, None) if fixed_stage_raw else None
        for node_id in sorted(terminal):
            n = nodi.get(node_id)
            if not n:
                continue
            if node_id in extra_outfall_by_id:
                stage = extra_outfall_by_id[node_id].get("stage")
                gated = extra_outfall_by_id[node_id].get("gated", "NO") or "NO"
            else:
                stage = fixed_stage
                gated = "NO"
            if stage is not None:
                a(f"{node_id:<16} {n['quota_fondo']:<12.3f} {'FIXED':<10} {float(stage):<15.3f} {gated:<8}")
            else:
                a(f"{node_id:<16} {n['quota_fondo']:<12.3f} {'FREE':<10} {'':<15} {gated:<8}")
        a("")

        if storage_def:
            storage_node = storage_def["node_id"]
            n = nodi[storage_node]
            curve_name = storage_def["curve_name"]
            storage_max_depth = max(float(n["max_depth"]), max(depth for depth, _area in storage_def["curve"]))
            a("[STORAGE]")
            a(";;Name           Elevation     MaxDepth     InitDepth    Shape      CurveName       SurDepth    Fevap")
            a(f"{storage_node:<16} {n['quota_fondo']:<12.3f} {storage_max_depth:<12.3f} {0:<12.3f} {'TABULAR':<10} {curve_name:<15} {0:<12.3f} {0:<8.3f}")
            a("")

        a("[CONDUITS]")
        a(";;Name           FromNode        ToNode          Length       Roughness    InOffset     OutOffset    InitFlow    MaxFlow")
        for c in conduits:
            if str(c["cond_id"]) in regulator_replace_links:
                continue
            a(f"{c['cond_id']:<16} {c['from_node']:<15} {c['to_node']:<15} {c['length']:<12.3f} {c['roughness']:<12.5f} {c['in_offset']:<12.3f} {c['out_offset']:<12.3f} {0:<12.3f} {0:<12.3f}")
        a("")

        if orifice_defs:
            a("[ORIFICES]")
            a(";;Name           FromNode        ToNode          Type       Offset      Qcoeff      Gated    CloseTime")
            for o in orifice_defs:
                offset = float(o.get("offset", 0.0) or 0.0)
                a(
                    f"{o['link_id']:<16} {o['from_node']:<15} {o['to_node']:<15} "
                    f"{o['type']:<10} {offset:<11.3f} {float(o['coeff']):<11.3f} {'NO':<8} {0:<10.3f}"
                )
            a("")

        if weir_defs:
            a("[WEIRS]")
            a(";;Name           FromNode        ToNode          Type          CrestHt     Qcoeff      Gated    EndCon   EndCoeff   Surcharge")
            for w in weir_defs:
                crest_offset = float(w.get("offset", w.get("height", 0.0)) or 0.0)
                a(
                    f"{w['link_id']:<16} {w['from_node']:<15} {w['to_node']:<15} "
                    f"{w['type']:<13} {crest_offset:<11.3f} {float(w['coeff']):<11.3f} {'NO':<8} {int(float(w.get('endcon', 0))):<8} {0:<10.3f} {'NO':<10}"
                )
            a("")

        if pump_defs:
            a("[PUMPS]")
            a(";;Name           FromNode        ToNode          PumpCurve       Status     Startup     Shutoff")
            for p in pump_defs:
                a(
                    f"{p['pump_id']:<16} {p['from_node']:<15} {p['to_node']:<15} "
                    f"{p['curve']:<15} {p['status']:<10} {float(p['startup']):<11.3f} {float(p['shutoff']):<11.3f}"
                )
            a("")

        a("[XSECTIONS]")
        a(";;Link           Shape        Geom1       Geom2       Geom3       Geom4       Barrels")
        for c in conduits:
            if str(c["cond_id"]) in regulator_replace_links:
                continue
            shape = str(c.get("shape") or "CIRCULAR").upper()
            geom1 = float(c.get("geom1", c.get("diam_m", 0.3)) or c.get("diam_m", 0.3) or 0.3)
            geom2 = float(c.get("geom2", 0.0) or 0.0)
            geom3 = float(c.get("geom3", 0.0) or 0.0)
            geom4 = float(c.get("geom4", 0.0) or 0.0)
            a(f"{c['cond_id']:<16} {shape:<12} {geom1:<11.3f} {geom2:<11.3f} {geom3:<11.3f} {geom4:<11.3f} {1:<11}")
        for o in orifice_defs:
            geom2 = o["width"] if o["shape"] != "CIRCULAR" else 0.0
            a(f"{o['link_id']:<16} {o['shape']:<12} {float(o['height']):<11.3f} {float(geom2):<11.3f} {0:<11.3f} {0:<11.3f} {1:<11}")
        for w in weir_defs:
            a(f"{w['link_id']:<16} {w['shape']:<12} {float(w['width']):<11.3f} {float(w['height']):<11.3f} {0:<11.3f} {0:<11.3f} {1:<11}")
        a("")

        a("[TIMESERIES]")
        a(";;Name           Date            Time            Value")
        for minute, intensity in rain_series:
            a(f"TS_PIOGGIA       01/01/2026      {self._format_swmm_time(minute)}           {float(intensity):.3f}")
        a("")

        if storage_def:
            curve_name = storage_def["curve_name"]
            a("[CURVES]")
            a(";;Name           Type       X-Value      Y-Value")
            first = True
            for depth, area in storage_def["curve"]:
                curve_type = "STORAGE" if first else ""
                a(f"{curve_name:<16} {curve_type:<10} {depth:<12.3f} {area:<12.3f}")
                first = False
            a("")

        if pump_defs:
            curve_written = set()
            if not storage_def:
                a("[CURVES]")
                a(";;Name           Type       X-Value      Y-Value")
            curve_types = {}
            for p in pump_defs:
                curve_name = str(p["curve"])
                curve_type = str(p.get("curve_type", "PUMP3"))
                if curve_name in curve_types and curve_types[curve_name] != curve_type:
                    raise Exception(f"Pump curve '{curve_name}' usata con tipi diversi: {curve_types[curve_name]} e {curve_type}.")
                curve_types[curve_name] = curve_type
            for p in pump_defs:
                curve_name = str(p["curve"])
                if curve_name in curve_written:
                    continue
                curve_written.add(curve_name)
                first = True
                for x, y in p["curve_points"]:
                    curve_type = str(p.get("curve_type", "PUMP3")) if first else ""
                    a(f"{curve_name:<16} {curve_type:<10} {x:<12.3f} {y:<12.3f}")
                    first = False
            a("")

        control_lines = []
        for o in orifice_defs:
            if o.get("rules"):
                if control_lines:
                    control_lines.append("")
                control_lines.extend(o["rules"])
        for w in weir_defs:
            if w.get("rules"):
                if control_lines:
                    control_lines.append("")
                control_lines.extend(w["rules"])
        if pump_defs:
            pump_rules = pump_defs[0].get("rules") or []
            if pump_rules:
                if control_lines:
                    control_lines.append("")
                control_lines.extend(pump_rules)
        if control_lines:
            a("[CONTROLS]")
            for line in control_lines:
                a(line)
            a("")

        a("[REPORT]")
        a("INPUT      NO")
        a(f"CONTROLS   {'YES' if control_lines else 'NO'}")
        a("SUBCATCHMENTS ALL")
        a("NODES ALL")
        a("LINKS ALL")
        a("")

        a("[COORDINATES]")
        a(";;Node           X-Coord         Y-Coord")
        for node_id, n in nodi.items():
            a(f"{node_id:<16} {n['x']:<15.3f} {n['y']:<15.3f}")
        a("")

        a("[VERTICES]")
        a(";;Link           X-Coord         Y-Coord")
        for c in conduits:
            if str(c["cond_id"]) in regulator_replace_links:
                continue
            verts = c.get("vertices") or []
            # In SWMM, link endpoints are already defined by nodes in
            # [COORDINATES]; only optional intermediate vertices are written here.
            if len(verts) > 2:
                for x, y in verts[1:-1]:
                    a(f"{c['cond_id']:<16} {float(x):<15.3f} {float(y):<15.3f}")
        for o in orifice_defs:
            verts = o.get("vertices") or []
            if len(verts) > 2:
                for x, y in verts[1:-1]:
                    a(f"{o['link_id']:<16} {float(x):<15.3f} {float(y):<15.3f}")
        for w in weir_defs:
            verts = w.get("vertices") or []
            if len(verts) > 2:
                for x, y in verts[1:-1]:
                    a(f"{w['link_id']:<16} {float(x):<15.3f} {float(y):<15.3f}")
        for p in pump_defs:
            verts = p.get("vertices") or []
            if len(verts) > 2:
                for x, y in verts[1:-1]:
                    a(f"{p['pump_id']:<16} {float(x):<15.3f} {float(y):<15.3f}")
        a("")

        a("[POLYGONS]")
        a(";;Subcatchment   X-Coord         Y-Coord")
        for s in subs:
            geom = s["geometry"]
            poly = geom.asPolygon()
            if not poly and geom.isMultipart():
                mp = geom.asMultiPolygon()
                poly = mp[0] if mp else []
            if poly:
                for p in poly[0]:
                    a(f"{s['sub_id']:<16} {p.x():<15.3f} {p.y():<15.3f}")
        a("")

        a("[SYMBOLS]")
        a(";;Gage           X-Coord         Y-Coord")
        c = basin_geom.centroid().asPoint()
        a(f"RG1              {c.x():<15.3f} {c.y():<15.3f}")
        a("")
        # SWMM does not require the [END] section; some solvers report it as an invalid keyword.
        return "\n".join(lines)

    def run_swmm_qgis_python(self, inp_file, rpt_file, out_file):
        """Run SWMM using the internal QGIS Python environment.

        Requires swmm-toolkit to be installed in the same Python environment used
        by QGIS. Supports both solver.run and solver.swmm_run API variants.
        """
        self.log_msg('Starting SWMM simulation with the internal QGIS Python environment...')
        self.log_msg(f"INP: {inp_file}")
        self.log_msg(f"RPT: {rpt_file}")
        self.log_msg(f"OUT: {out_file}")

        try:
            from swmm.toolkit import solver
        except Exception as e:
            raise Exception(
                "swmm-toolkit is not installed in the internal QGIS Python environment or cannot be imported. "
                'Install swmm-toolkit in the QGIS Python environment and try again. Original error: '
                f"{e}"
            )

        try:
            if hasattr(solver, "run"):
                solver.run(inp_file, rpt_file, out_file)
            elif hasattr(solver, "swmm_run"):
                solver.swmm_run(inp_file, rpt_file, out_file)
            else:
                raise AttributeError(
                    "The installed swmm-toolkit library contains neither solver.run nor solver.swmm_run."
                )
        except Exception as e:
            raise Exception(
                "The SWMM simulation returned an error. "
                'Check the log and the .rpt file. Original error: '
                f"{e}"
            )

        self.log_msg('SWMM simulation completed with the internal QGIS Python environment.')

    def run_swmm_external(self, python_exe, runner, inp_file, rpt_file, out_file):
        cmd = [python_exe, runner, inp_file, rpt_file, out_file]

        # QGIS/OSGeo4W sets its own Python environment variables.
        # If inherited by the external Python environment, Python 3.13 may try to
        # load libraries from the QGIS Python runtime, for example Python 3.7, causing errors
        # such as "SRE module mismatch" or importlib._bootstrap_external._w_long.
        env = os.environ.copy()
        for key in (
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
        ):
            env.pop(key, None)

        py_dir = os.path.dirname(python_exe)
        py_scripts = os.path.join(py_dir, "Scripts")
        old_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([py_dir, py_scripts, old_path])

        self.log_msg('Starting SWMM simulation with external Python...')
        self.log_msg(" ".join([f'"{x}"' if " " in x else x for x in cmd]))
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.stdout:
            self.log_msg(result.stdout)
        if result.stderr:
            self.log_msg(result.stderr)
        if result.returncode != 0:
            raise Exception('The SWMM simulation returned an error. Check the log and the .rpt file.')


    def safe_float(self, value, default=None):
        try:
            return float(str(value).replace(",", "."))
        except Exception:
            return default

    def parse_swmm_rpt_results(self, rpt_file):
        """Read the maximum summary results produced by SWMM from the .rpt file.

        Extracted sections:
        - Link Flow Summary: Maximum Flow, Maximum Velocity, Maximum Depth and
          Max/Full Depth for each conduit;
        - Node Depth Summary: Maximum Depth and Maximum HGL for each node;
        - Node Flooding Summary: total flooded volume and maximum flooding rate
          for flooded nodes.

        Unit note: with FLOW_UNITS = LPS, link flow is expressed in L/s and the
        flooded volume reported by SWMM is normally expressed in 10^6 liters; it
        is therefore also converted to m³ using a factor of 1000. Nodes missing
        from the Flooding Summary are still loaded into the result layer with
        flooded volume equal to 0.
        """
        link_results = {}
        node_results = {}

        if not os.path.exists(rpt_file):
            raise Exception(f"File RPT non trovato: {rpt_file}")

        with open(rpt_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        def data_lines_after(title):
            start = None
            for i, line in enumerate(lines):
                if title.lower() in line.lower():
                    start = i
                    break
            if start is None:
                return []

            # Search for the last separator line before the data.
            sep_count = 0
            data_start = None
            for j in range(start + 1, min(len(lines), start + 80)):
                txt = lines[j].strip()
                if txt and set(txt) <= set("- ") and "-" in txt:
                    sep_count += 1
                    data_start = j + 1
                    if sep_count >= 2:
                        break
            if data_start is None:
                data_start = start + 1

            out = []
            for j in range(data_start, len(lines)):
                raw = lines[j].rstrip("\n")
                txt = raw.strip()
                if not txt:
                    if out:
                        break
                    continue
                low = txt.lower()
                if "summary" in low and len(out) > 0:
                    break
                if txt.startswith("*") or txt.startswith("["):
                    if out:
                        break
                    continue
                if set(txt) <= set("- ") and "-" in txt:
                    continue
                if txt.startswith(";"):
                    continue
                out.append(txt)
            return out

        def numeric_tokens(tokens):
            vals = []
            for t in tokens:
                if re.match(r"^[+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?$", t):
                    vals.append(float(t))
            return vals

        def section_header_text(title):
            start = None
            for i, line in enumerate(lines):
                if title.lower() in line.lower():
                    start = i
                    break
            if start is None:
                return ""
            header_lines = []
            sep_count = 0
            for j in range(start + 1, min(len(lines), start + 80)):
                txt = lines[j].rstrip("\n")
                header_lines.append(txt)
                st = txt.strip()
                if st and set(st) <= set("- ") and "-" in st:
                    sep_count += 1
                    if sep_count >= 2:
                        break
            return "\n".join(header_lines).lower()

        # Link Flow Summary. Some SWMM versions/reports directly provide
        # the Maximum Depth column in length units; in that case, hmax_m is
        # read directly from the report. If the column does not exist,
        # Max/Full Depth is interpreted as a relative filling ratio.
        link_header = section_header_text("Link Flow Summary")
        has_absolute_depth_col = ("maximum" in link_header and "depth" in link_header and "max/full" not in link_header.replace(" ", "")) or ("maximum depth" in link_header) or ("max depth" in link_header)

        for txt in data_lines_after("Link Flow Summary"):
            parts = txt.split()
            if len(parts) < 4:
                continue
            link_id = parts[0]
            if link_id.lower() in ("link", "name", "----"):
                continue
            vals = numeric_tokens(parts[1:])
            qmax = vals[0] if len(vals) >= 1 else None
            hmax_m = None
            vmax = None
            max_full_depth = None

            if has_absolute_depth_col and len(vals) >= 6:
                # Schema atteso: MaxFlow, day, MaximumDepth, MaxVelocity, Max/FullFlow, Max/FullDepth
                hmax_m = vals[2]
                vmax = vals[3]
                max_full_depth = vals[5]
            else:
                # Schema classico: MaxFlow, day, MaxVelocity, Max/FullFlow, Max/FullDepth
                vmax = vals[2] if len(vals) >= 3 else (vals[1] if len(vals) >= 2 else None)
                max_full_depth = vals[4] if len(vals) >= 5 else None

            if qmax is not None or vmax is not None or hmax_m is not None:
                link_results[link_id] = {
                    "qmax_lps": qmax,
                    "vmax_ms": vmax,
                    "hmax_m": hmax_m,
                    "max_full_depth": max_full_depth,
                }


        # Node Depth Summary: dati tipici:
        # Node Type AverageDepth MaximumDepth TimeDay TimeHr ReportedMaxDepth MaxHGL
        # Used to assign a maximum value also to nodes that are not listed as flooded.
        for txt in data_lines_after("Node Depth Summary"):
            parts = txt.split()
            if len(parts) < 4:
                continue
            node_id = parts[0]
            if node_id.lower() in ("node", "name", "----"):
                continue
            vals = numeric_tokens(parts[1:])
            if len(vals) < 2:
                continue

            avg_depth = vals[0] if len(vals) >= 1 else None
            max_depth = vals[1] if len(vals) >= 2 else None
            max_hgl = vals[-1] if len(vals) >= 4 else None

            node_results.setdefault(node_id, {})
            node_results[node_id].update({
                "avg_depth_m": avg_depth,
                "max_depth_m": max_depth,
                "max_hgl_m": max_hgl,
            })

        # Node Flooding Summary: dati tipici:
        # Node HoursFlooded MaxRate TimeDay TimeHr TotalFloodVolume
        for txt in data_lines_after("Node Flooding Summary"):
            parts = txt.split()
            if len(parts) < 3:
                continue
            node_id = parts[0]
            if node_id.lower() in ("node", "name", "----"):
                continue
            vals = numeric_tokens(parts[1:])
            if not vals:
                continue
            hours = vals[0] if len(vals) >= 1 else None
            max_rate = vals[1] if len(vals) >= 2 else None
            volume_raw = vals[-1] if len(vals) >= 3 else None
            volume_m3 = volume_raw * 1000.0 if volume_raw is not None else None
            node_results.setdefault(node_id, {})
            node_results[node_id].update({
                "flood_h": hours,
                "flood_lps": max_rate,
                "flood_10e6l": volume_raw,
                "flood_m3": volume_m3,
            })

        return link_results, node_results

    def unique_field_name(self, existing, desired):
        name = desired[:10]
        if name not in existing:
            existing.add(name)
            return name
        base = name[:8]
        i = 1
        while True:
            candidate = f"{base}_{i}"[:10]
            if candidate not in existing:
                existing.add(candidate)
                return candidate
            i += 1

    def make_memory_layer_like(self, source_layer, name, extra_fields):
        geom_name = QgsWkbTypes.displayString(source_layer.wkbType())
        crs_auth = source_layer.crs().authid()
        layer = QgsVectorLayer(f"{geom_name}?crs={crs_auth}", name, "memory")
        pr = layer.dataProvider()
        fields = QgsFields()
        existing = set()
        for fld in source_layer.fields():
            fields.append(fld)
            existing.add(fld.name())
        actual_extra = []
        for fname, ftype in extra_fields:
            unique = self.unique_field_name(existing, fname)
            fields.append(QgsField(unique, ftype))
            actual_extra.append(unique)
        pr.addAttributes(fields)
        layer.updateFields()
        return layer, actual_extra

    def _make_result_symbol(self, geom_type, color, width=None, size=None):
        symbol = QgsSymbol.defaultSymbol(geom_type)
        if symbol is None:
            return None
        symbol.setColor(QColor(color))
        try:
            if width is not None and hasattr(symbol, "setWidth"):
                symbol.setWidth(float(width))
        except Exception:
            pass
        try:
            if size is not None and hasattr(symbol, "setSize"):
                symbol.setSize(float(size))
        except Exception:
            pass
        return symbol

    def apply_condotte_results_style(self, layer):
        """Style conduit result layers by maximum filling percentage hmax_pct."""
        try:
            ranges = []
            specs = [
                (0.0, 50.0, "≤ 50% - regolare", "#2c7bb6", 0.45),
                (50.0, 80.0, "50–80% - warning", "#1a9641", 0.65),
                (80.0, 100.0, "80–100% - critico", "#fdae61", 0.90),
                (100.0, 999999.0, "> 100% / in pressione", "#d7191c", 1.20),
            ]
            for low, high, label, color, width in specs:
                sym = self._make_result_symbol(layer.geometryType(), color, width=width)
                if sym is not None:
                    ranges.append(QgsRendererRange(low, high, sym, label))
            renderer = QgsGraduatedSymbolRenderer("hmax_pct", ranges)
            renderer.setMode(QgsGraduatedSymbolRenderer.Custom)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.log_msg("Style applied to condotte_swmm_risultati: color by hmax_pct.")
        except Exception as e:
            self.log_msg(f"Avviso: impossibile applicare lo stile alle condotte risultati: {e}")

    def apply_nodi_results_style(self, layer):
        """Style node result layers by maximum flooding flow flood_lps."""
        try:
            ranges = []
            specs = [
                (0.0, 0.000001, "No flooding", "#2c7bb6", 2.2),
                (0.000001, 10.0, "0–10 L/s", "#ffffbf", 3.2),
                (10.0, 50.0, "10–50 L/s", "#fdae61", 4.6),
                (50.0, 999999999.0, "> 50 L/s", "#d7191c", 6.2),
            ]
            for low, high, label, color, size in specs:
                sym = self._make_result_symbol(layer.geometryType(), color, size=size)
                if sym is not None:
                    ranges.append(QgsRendererRange(low, high, sym, label))
            renderer = QgsGraduatedSymbolRenderer("flood_lps", ranges)
            renderer.setMode(QgsGraduatedSymbolRenderer.Custom)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            self.log_msg("Style applied to nodi_swmm_risultati: color by flood_lps.")
        except Exception as e:
            self.log_msg(f"Avviso: impossibile applicare lo stile ai nodi risultati: {e}")


    def _safe_filename_part(self, value):
        txt = str(value) if value is not None else ""
        txt = re.sub(r"[^A-Za-z0-9_\-]+", "_", txt).strip("_")
        return txt or "profilo"

    def _node_id_from_feature(self, f):
        v = self.attr(f, ["node_id", "NODE_ID", "id", "Id", "ID", "nome", "Name"])
        return str(v) if v is not None else f"N{f.id()}"

    def _conduit_id_from_feature(self, f):
        v = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
        return str(v) if v is not None else f"COND_{f.id()}"

    def _profile_link_length(self, link_def, nodes):
        """Compute an indicative length for regulator links drawn in profile views."""
        verts = link_def.get("vertices") or []
        try:
            pts = [(float(x), float(y)) for x, y in verts]
        except Exception:
            pts = []
        if len(pts) >= 2:
            total = 0.0
            for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
                total += math.hypot(x1 - x0, y1 - y0)
            if total > 0:
                return total
        n0 = nodes.get(str(link_def.get("from_node")), {})
        n1 = nodes.get(str(link_def.get("to_node")), {})
        if n0.get("x") is not None and n0.get("y") is not None and n1.get("x") is not None and n1.get("y") is not None:
            return math.hypot(float(n1["x"]) - float(n0["x"]), float(n1["y"]) - float(n0["y"]))
        return 1.0

    def _merge_profile_regulator_links(self, conduits, nodes, orifice_defs=None, pump_defs=None, weir_defs=None):
        """Merge pumps, orifices and weirs into the link list used by profile views.

        Regulators created by converting an existing conduit retain the position
        of the original conduit but are drawn with a dedicated symbol/type.
        Regulators drawn from scratch are added as links between the selected
        nodes.
        """
        out = [dict(c) for c in (conduits or [])]
        by_id = {str(c.get("cond_id")): i for i, c in enumerate(out)}

        def base_link(item, link_id, link_type):
            from_node = str(item.get("from_node", ""))
            to_node = str(item.get("to_node", ""))
            repl = str(item.get("replace_link", "") or "")
            base = None
            if repl and repl in by_id:
                base = dict(out[by_id[repl]])
            d = base or {}
            d["cond_id"] = str(link_id)
            d["from_node"] = from_node or str(d.get("from_node", ""))
            d["to_node"] = to_node or str(d.get("to_node", ""))
            d["link_type"] = link_type
            d["replace_link"] = repl
            if item.get("vertices"):
                d["vertices"] = list(item.get("vertices") or [])
            if not d.get("length") or float(d.get("length") or 0) <= 0:
                d["length"] = self._profile_link_length(d, nodes)
            if d.get("z_monte") is None:
                d["z_monte"] = nodes.get(d.get("from_node"), {}).get("invert")
            if d.get("z_valle") is None:
                d["z_valle"] = nodes.get(d.get("to_node"), {}).get("invert")
            return d, repl

        def upsert(item, link_id, link_type, display_depth=None, extra=None):
            d, repl = base_link(item, link_id, link_type)
            if display_depth is not None:
                try:
                    d["diam_m"] = max(0.05, float(display_depth))
                except Exception:
                    d["diam_m"] = 0.30
            else:
                d["diam_m"] = float(d.get("diam_m") or 0.30)
            d["regulator_label"] = extra or ""
            d["qmax_lps"] = d.get("qmax_lps")
            d["vmax_ms"] = d.get("vmax_ms")
            d["hmax_m"] = d.get("hmax_m")
            d["hmax_pct"] = d.get("hmax_pct")
            if repl and repl in by_id:
                out[by_id[repl]] = d
                by_id[str(link_id)] = by_id[repl]
            elif str(link_id) in by_id:
                out[by_id[str(link_id)]].update(d)
            else:
                by_id[str(link_id)] = len(out)
                out.append(d)

        for o in (orifice_defs or getattr(self, "orifice_definitions", []) or []):
            lid = str(o.get("link_id") or o.get("replace_link") or "ORIFICE")
            height = o.get("height", o.get("geom1", 0.30))
            label = f"{o.get('type','ORIFICE')} / {o.get('shape','')}"
            upsert(o, lid, "ORIFICE", height, label)
        for w in (weir_defs or getattr(self, "weir_definitions", []) or []):
            lid = str(w.get("link_id") or w.get("replace_link") or "WEIR")
            height = w.get("height", 0.30)
            label = f"{w.get('type','WEIR')} / crest={float(height):.2f} m"
            upsert(w, lid, "WEIR", max(float(height or 0.30), 0.20), label)
        for pmp in (pump_defs or getattr(self, "pump_definitions", []) or []):
            lid = str(pmp.get("pump_id") or pmp.get("replace_link") or "PUMP")
            label = f"{pmp.get('curve_type','PUMP')} / {pmp.get('curve','')}"
            upsert(pmp, lid, "PUMP", 0.35, label)
        return out

    def _profile_link_symbol(self, ax, x0, x1, z0, z1, link_type, label=None):
        """Draw a simple symbol for pumps, orifices and weirs in static profiles."""
        link_type = str(link_type or "CONDUIT").upper()
        xm = (float(x0) + float(x1)) / 2.0
        ym = (float(z0) + float(z1)) / 2.0
        if link_type == "PUMP":
            ax.plot([x0, x1], [z0, z1], color="#7b3294", linewidth=2.2, linestyle="-")
            ax.scatter([xm], [ym], s=180, facecolors="white", edgecolors="#7b3294", linewidths=2.0, zorder=6)
            ax.text(xm, ym, "P", ha="center", va="center", fontsize=8, color="#7b3294", fontweight="bold", zorder=7)
        elif link_type == "ORIFICE":
            ax.plot([x0, x1], [z0, z1], color="#d95f02", linewidth=2.0, linestyle="-")
            ax.scatter([xm], [ym], marker="s", s=120, facecolors="white", edgecolors="#d95f02", linewidths=2.0, zorder=6)
            ax.text(xm, ym, "O", ha="center", va="center", fontsize=8, color="#d95f02", fontweight="bold", zorder=7)
        elif link_type == "WEIR":
            ax.plot([x0, x1], [z0, z1], color="#1b9e77", linewidth=2.0, linestyle="-")
            ax.scatter([xm], [ym], marker="^", s=150, facecolors="white", edgecolors="#1b9e77", linewidths=2.0, zorder=6)
            ax.text(xm, ym, "W", ha="center", va="center", fontsize=8, color="#1b9e77", fontweight="bold", zorder=7)
        if label:
            ax.annotate(str(label), (xm, ym), xytext=(0, -18), textcoords="offset points", ha="center", fontsize=7,
                        bbox=dict(boxstyle="round,pad=0.20", fc="white", ec="#aaaaaa", alpha=0.90))

    def _build_profile_paths(self, conduits):
        """Build upstream-to-downstream paths used for result profile views.

        Each network source generates a downstream profile. In a typical sewer
        network, this produces one image for each pipe path.
        """
        outgoing = {}
        incoming = {}
        nodes = set()
        for c in conduits:
            fn = c.get("from_node")
            tn = c.get("to_node")
            if not fn or not tn:
                continue
            outgoing.setdefault(fn, []).append(c)
            incoming[tn] = incoming.get(tn, 0) + 1
            nodes.add(fn); nodes.add(tn)

        for lst in outgoing.values():
            lst.sort(key=lambda x: (str(x.get("to_node")), str(x.get("cond_id"))))

        sources = sorted([n for n in nodes if outgoing.get(n) and incoming.get(n, 0) == 0], key=str)
        if not sources:
            sources = sorted([n for n in outgoing.keys()], key=str)

        paths = []
        max_paths = 80

        def dfs(node, path, seen):
            if len(paths) >= max_paths:
                return
            nexts = outgoing.get(node, [])
            if not nexts:
                if path:
                    paths.append(list(path))
                return
            progressed = False
            for c in nexts:
                cid = c.get("cond_id")
                tn = c.get("to_node")
                key = (cid, tn)
                if key in seen:
                    continue
                progressed = True
                path.append(c)
                seen.add(key)
                dfs(tn, path, seen)
                seen.remove(key)
                path.pop()
            if not progressed and path:
                paths.append(list(path))

        for src in sources:
            dfs(src, [], set())

        # Deduplicate identical paths.
        unique = []
        seen_keys = set()
        for pth in paths:
            key = tuple(c.get("cond_id") for c in pth)
            if key and key not in seen_keys:
                seen_keys.add(key)
                unique.append(pth)
        return unique

    def _profile_color_for_pct(self, pct):
        """Return the SWMM criticality color for a filling percentage."""
        try:
            pct = float(pct)
        except Exception:
            return "#4a90e2"
        if pct < 50:
            return "#2c7bb6"
        if pct < 80:
            return "#1a9641"
        if pct <= 100:
            return "#fdae61"
        return "#d7191c"

    def _write_profile_html_viewer(self, profile_dir, profiles_payload):
        """Create a small interactive HTML viewer for generated profiles."""
        try:
            out_html = os.path.join(profile_dir, "viewer_profili_swmm_qmax.html")
            payload = json.dumps(profiles_payload, ensure_ascii=False)
            html = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>SWMM Qmax Profiles</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 18px; background:#fafafa; color:#222; }}
#wrap {{ max-width: 1200px; margin:auto; }}
.card {{ background:white; border:1px solid #ddd; border-radius:10px; padding:14px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
img {{ max-width:100%; border:1px solid #ddd; background:white; }}
.controls {{ display:flex; gap:12px; align-items:center; margin: 12px 0; flex-wrap:wrap; }}
.note {{ color:#666; font-size: 13px; line-height: 1.4; }}
.badge {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#eef; }}
</style>
</head>
<body>
<div id=\"wrap\" class=\"card\">
<h2>SWMM Profiles - Qmax / hmax / filling</h2>
<div class=\"controls\">
<label>Profile: <select id=\"sel\"></select></label>
<button onclick=\"prev()\">◀</button>
<button onclick=\"next()\">▶</button>
<span id=\"info\" class=\"badge\"></span>
</div>
<img id=\"img\" alt=\"SWMM profile\">
<p class=\"note\">This viewer shows the critical profiles generated from the SWMM report: Qmax, hmax and hmax_pct. The timestep-by-timestep dynamic view is created separately in the dynamic_swmm_profiles folder when the .OUT file can be read.</p>
</div>
<script>
const profiles = {payload};
let idx = 0;
const sel = document.getElementById('sel');
const img = document.getElementById('img');
const info = document.getElementById('info');
profiles.forEach((p,i)=>{{ const o=document.createElement('option'); o.value=i; o.textContent=p.title; sel.appendChild(o); }});
function show(i) {{
  if (!profiles.length) return;
  idx = (i + profiles.length) % profiles.length;
  sel.value = idx;
  img.src = profiles[idx].file;
  info.textContent = (idx+1) + ' / ' + profiles.length;
}}
function prev() {{ show(idx-1); }}
function next() {{ show(idx+1); }}
sel.onchange = () => show(parseInt(sel.value));
show(0);
</script>
</body>
</html>"""
            with open(out_html, "w", encoding="utf-8") as f:
                f.write(html)
            return out_html
        except Exception as e:
            self.log_msg(f"Warning: unable to create SWMM profile HTML viewer: {e}")
            return None

    def create_swmm_profile_images(self, cond_layer, nodi_layer, link_results, outdir, out_file=None, orifice_defs=None, pump_defs=None, weir_defs=None):
        """Create PNG images of SWMM profiles in hydraulic profile style.

        Each pipe path is represented with terrain, manholes, conduits, water
        filling proportional to hmax_m/hmax_pct, Qmax_lps and hmax_pct. Values
        available from the report are summary maxima; true time-based animation
        requires time series from the .OUT file.
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from matplotlib.patches import Polygon
        except Exception as e:
            self.log_msg(f"Avviso: impossibile creare i profili immagine, matplotlib non disponibile: {e}")
            return []

        profile_dir = os.path.join(outdir, "profili_swmm_qmax")
        os.makedirs(profile_dir, exist_ok=True)

        nodes = {}
        for f in nodi_layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            nid = self._node_id_from_feature(f)
            pt = g.asPoint()
            ground = self.attr_float(f, ["elevaz", "ELEVAZ", "quota_terr", "ground_elevation", "ELEVATION", "q_terr"], None)
            invert = self.attr_float(f, ["q_scorr", "Q_SCORR", "quota_fondo", "invert", "invert_elevation", "q_fondo"], None)
            prof = self.attr_float(f, ["prof_scav", "PROF_SCAV", "excavation_depth", "max_depth", "depth"], None)
            if ground is None and invert is not None and prof is not None:
                ground = invert + prof
            nodes[nid] = {"ground": ground, "invert": invert, "x": pt.x(), "y": pt.y()}

        conduits = []
        for f in cond_layer.getFeatures():
            cid = self._conduit_id_from_feature(f)
            fn = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
            tn = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])
            if fn is None or tn is None:
                continue
            fn = str(fn); tn = str(tn)
            length = self.attr_float(f, ["Lenght", "LENGHT", "length", "Length", "LENGTH", "lunghezza"], None)
            geom = f.geometry()
            if (length is None or length <= 0) and geom and not geom.isEmpty():
                length = geom.length()
            length = max(float(length or 0.0), 0.0)
            diam_mm = self.attr_float(f, ["diam_mm", "DN", "diametro", "D_mm"], None)
            diam_m = self.attr_float(f, ["D", "diam_m"], None)
            if diam_m is None:
                diam_m = (diam_mm / 1000.0) if diam_mm else None
            z_monte = self.attr_float(f, ["z_monte", "Z_MONTE", "scorr_monte", "q_monte"], None)
            z_valle = self.attr_float(f, ["z_valle", "Z_VALLE", "scorr_valle", "q_valle"], None)
            if z_monte is None:
                z_monte = nodes.get(fn, {}).get("invert")
            if z_valle is None:
                z_valle = nodes.get(tn, {}).get("invert")
            res = link_results.get(cid, {})
            hmax_m = res.get("hmax_m")
            max_full_depth = res.get("max_full_depth")
            if hmax_m is None and max_full_depth is not None and diam_m not in (None, 0):
                hmax_m = float(max_full_depth) * float(diam_m)
            if hmax_m is not None and diam_m not in (None, 0):
                hmax_pct = float(hmax_m) / float(diam_m) * 100.0
            elif max_full_depth is not None:
                hmax_pct = float(max_full_depth) * 100.0
            else:
                hmax_pct = None
            conduits.append({
                "cond_id": cid,
                "from_node": fn,
                "to_node": tn,
                "length": length,
                "diam_m": diam_m,
                "z_monte": z_monte,
                "z_valle": z_valle,
                "qmax_lps": res.get("qmax_lps"),
                "vmax_ms": res.get("vmax_ms"),
                "hmax_m": hmax_m,
                "hmax_pct": hmax_pct,
            })

        conduits = self._merge_profile_regulator_links(conduits, nodes, orifice_defs, pump_defs, weir_defs)
        paths = self._build_profile_paths(conduits)
        if not paths:
            self.log_msg('Warning: no valid path found to create SWMM profiles.')
            return []

        created = []
        viewer_payload = []
        for idx, path in enumerate(paths, start=1):
            if not path:
                continue
            x_vals = [0.0]
            node_seq = [path[0]["from_node"]]
            for c in path:
                x_vals.append(x_vals[-1] + float(c.get("length") or 0.0))
                node_seq.append(c.get("to_node"))

            terrain = [nodes.get(n, {}).get("ground") for n in node_seq]
            invert_nodes = [nodes.get(n, {}).get("invert") for n in node_seq]
            q_values = [c.get("qmax_lps") for c in path if c.get("qmax_lps") is not None]
            branch_qmax = max(q_values) if q_values else None
            branch_q_link = None
            if branch_qmax is not None:
                for c in path:
                    if c.get("qmax_lps") == branch_qmax:
                        branch_q_link = c.get("cond_id")
                        break

            fig, ax = plt.subplots(figsize=(13.0, 5.8), dpi=160)
            fig.patch.set_facecolor("white")
            ax.set_facecolor("#fbfbfb")
            ax.grid(True, which="major", color="#d9d9d9", linewidth=0.6)
            ax.grid(True, which="minor", color="#eeeeee", linewidth=0.4)
            ax.minorticks_on()

            if any(v is not None for v in terrain):
                ax.plot(x_vals, [float(v) if v is not None else math.nan for v in terrain], color="#333333", marker="o", markersize=4, linewidth=1.6, label="Terreno")
            if any(v is not None for v in invert_nodes):
                ax.plot(x_vals, [float(v) if v is not None else math.nan for v in invert_nodes], color="#666666", marker="s", markersize=3, linewidth=1.0, linestyle="--", label='Manhole invert')

            for x, nid in zip(x_vals, node_seq):
                zt = nodes.get(nid, {}).get("ground")
                zi = nodes.get(nid, {}).get("invert")
                if zt is not None and zi is not None:
                    ax.plot([x, x], [float(zi), float(zt)], color="#444444", linewidth=1.0)
                    ax.annotate(str(nid), (x, float(zt)), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=8, color="#111111")

            for i, c in enumerate(path):
                x0, x1 = x_vals[i], x_vals[i + 1]
                z0 = c.get("z_monte") or nodes.get(c.get("from_node"), {}).get("invert")
                z1 = c.get("z_valle") or nodes.get(c.get("to_node"), {}).get("invert")
                if z0 is None or z1 is None:
                    continue
                z0 = float(z0); z1 = float(z1)
                d = c.get("diam_m")
                d = float(d) if d not in (None, 0) else 0.30
                h = c.get("hmax_m")
                pct = c.get("hmax_pct")
                if h is None and pct is not None:
                    h = d * float(pct) / 100.0
                if h is None:
                    h = 0.0
                h_plot = max(0.0, min(float(h), d))
                color = self._profile_color_for_pct(pct)
                is_critical = (branch_q_link is not None and c.get("cond_id") == branch_q_link)
                link_type = str(c.get("link_type", "CONDUIT") or "CONDUIT").upper()

                if link_type == "CONDUIT":
                    pipe_poly = Polygon([(x0, z0), (x1, z1), (x1, z1 + d), (x0, z0 + d)], closed=True,
                                        facecolor="#f7f7f7", edgecolor=color, linewidth=2.2 if is_critical else 1.4, alpha=0.95)
                    ax.add_patch(pipe_poly)
                    if h_plot > 0:
                        water_poly = Polygon([(x0, z0), (x1, z1), (x1, z1 + h_plot), (x0, z0 + h_plot)], closed=True,
                                             facecolor="#7ec8e3", edgecolor="none", alpha=0.75)
                        ax.add_patch(water_poly)
                    if pct is not None and float(pct) > 100.0:
                        ax.plot([x0, x1], [z0 + d + 0.05, z1 + d + 0.05], color="#d7191c", linewidth=2.0, linestyle="--")
                else:
                    self._profile_link_symbol(ax, x0, x1, z0, z1, link_type, c.get("regulator_label"))

                xm = (x0 + x1) / 2.0
                ym = (z0 + z1) / 2.0 + d + 0.04
                label = f"{c.get('cond_id')}"
                if link_type != "CONDUIT":
                    label += f"\n{link_type}"
                if c.get("qmax_lps") is not None:
                    label += f"\nQmax={float(c['qmax_lps']):.2f} L/s"
                if c.get("hmax_m") is not None:
                    label += f"\nhmax={float(c['hmax_m']):.2f} m"
                if pct is not None:
                    label += f"\nhmax={float(pct):.1f}%"
                if link_type == "CONDUIT":
                    label += f"\nDN {int(round(d*1000))}"
                ax.annotate(label, (xm, ym), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=7.4,
                            bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#999999", alpha=0.92),
                            arrowprops=dict(arrowstyle="-", color="#777777", lw=0.6))

            start = node_seq[0]
            end = node_seq[-1]
            title = f"Critical SWMM profile {idx}: {start} → {end}"
            if branch_qmax is not None:
                title += f" | Qmax ramo = {branch_qmax:.2f} L/s"
                if branch_q_link:
                    title += f" ({branch_q_link})"
            ax.set_title(title, fontsize=11, fontweight="bold")
            ax.set_xlabel("Distanza progressiva [m]")
            ax.set_ylabel('Elevation [m]')
            ax.legend(loc="best")

            all_y = []
            for v in terrain + invert_nodes:
                if v is not None:
                    all_y.append(float(v))
            for c in path:
                for v in (c.get("z_monte"), c.get("z_valle")):
                    if v is not None:
                        all_y.append(float(v))
                        if c.get("diam_m"):
                            all_y.append(float(v) + float(c.get("diam_m")))
            if all_y:
                ymin, ymax = min(all_y), max(all_y)
                pad = max(0.5, (ymax - ymin) * 0.15)
                ax.set_ylim(ymin - pad, ymax + pad)
            fig.tight_layout()

            fname = f"profilo_swmm_critico_{idx:02d}_{self._safe_filename_part(start)}_{self._safe_filename_part(end)}.png"
            out_png = os.path.join(profile_dir, fname)
            fig.savefig(out_png)
            plt.close(fig)
            created.append(out_png)
            viewer_payload.append({"title": title, "file": fname})

        html = self._write_profile_html_viewer(profile_dir, viewer_payload) if viewer_payload else None
        if html:
            self.log_msg(f"Viewer HTML profili SWMM creato: {html}")
        self.log_msg(f"Profili SWMM critici creati: {len(created)} immagine/i in {profile_dir}")
        return created



    def _read_out_timeseries_with_pyswmm(self, out_file, link_ids, node_ids):
        """Read time series from the .OUT file using PySWMM, when available.

        Returns a dictionary with:
        - times: list of date/time strings
        - links: {link_id: {q, h, v, capacity}}
        - nodes: {node_id: {depth, head, flooding}}

        Note: PySWMM reads the binary .OUT file directly through the Output,
        LinkSeries and NodeSeries classes. If PySWMM is not installed in the QGIS
        Python environment, a clear exception is raised.
        """
        if not out_file or not os.path.exists(out_file):
            raise Exception(f"File OUT non trovato: {out_file}")

        try:
            from pyswmm import Output, LinkSeries, NodeSeries
        except Exception as e:
            raise Exception(
                "PySWMM must be available in the QGIS Python environment to create timestep-by-timestep dynamic profiles. "
                'Installa con: python -m pip install pyswmm. Error import: ' + str(e)
            )

        link_ids = [str(x) for x in link_ids if x]
        node_ids = [str(x) for x in node_ids if x]
        data = {"times": [], "links": {}, "nodes": {}}

        def _series_values(series_dict, base_times=None):
            if series_dict is None:
                return [], []
            try:
                items = list(series_dict.items())
            except Exception:
                return [], []
            items.sort(key=lambda kv: kv[0])
            times = [k for k, v in items]
            vals = []
            for k, v in items:
                try:
                    vals.append(float(v))
                except Exception:
                    vals.append(None)
            return times, vals

        def _to_str_times(times):
            out = []
            for t in times:
                try:
                    out.append(t.strftime("%Y-%m-%d %H:%M:%S"))
                except Exception:
                    out.append(str(t))
            return out

        with Output(out_file) as out:
            available_links = set(str(x) for x in getattr(out, "links", []))
            available_nodes = set(str(x) for x in getattr(out, "nodes", []))

            ls = LinkSeries(out)
            ns = NodeSeries(out)
            base_times = None

            for lid in link_ids:
                if available_links and lid not in available_links:
                    continue
                try:
                    obj = ls[lid]
                except Exception:
                    continue
                rec = {}
                for attr_name, key in (("flow_rate", "q"), ("flow_depth", "h"), ("flow_velocity", "v"), ("capacity", "capacity")):
                    try:
                        times, vals = _series_values(getattr(obj, attr_name))
                    except Exception:
                        times, vals = [], []
                    if base_times is None and times:
                        base_times = times
                    rec[key] = vals
                data["links"][lid] = rec

            for nid in node_ids:
                if available_nodes and nid not in available_nodes:
                    continue
                try:
                    obj = ns[nid]
                except Exception:
                    continue
                rec = {}
                for attr_name, key in (("invert_depth", "depth"), ("hydraulic_head", "head"), ("flooding_losses", "flooding")):
                    try:
                        times, vals = _series_values(getattr(obj, attr_name))
                    except Exception:
                        times, vals = [], []
                    if base_times is None and times:
                        base_times = times
                    rec[key] = vals
                data["nodes"][nid] = rec

        data["times"] = _to_str_times(base_times or [])
        return data

    def _write_dynamic_swmm_profile_html(self, dynamic_dir, payload):
        out_html = os.path.join(dynamic_dir, "dynamic_swmm_out_viewer.html")
        js_payload = json.dumps(payload, ensure_ascii=False)
        html = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Dynamic SWMM Profiles from OUT</title>
<style>
body {{ font-family: Arial, sans-serif; margin:18px; background:#f7f7f7; color:#222; }}
#wrap {{ max-width:1280px; margin:auto; background:#fff; border:1px solid #ddd; border-radius:12px; padding:14px; box-shadow:0 2px 10px rgba(0,0,0,.08); }}
.controls {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:10px 0 14px; }}
select,input,button {{ font-size:14px; padding:5px 8px; }}
#timeSlider {{ width:420px; }}
.badge {{ background:#eef; padding:4px 8px; border-radius:999px; }}
.note {{ color:#666; font-size:13px; line-height:1.4; }}
svg {{ width:100%; height:610px; border:1px solid #ddd; background:#fbfbfb; }}
.label {{ font-size:11px; fill:#111; }}
.small {{ font-size:10px; fill:#333; }}
.grid {{ stroke:#ddd; stroke-width:0.7; }}
.pipe {{ fill:#f7f7f7; stroke:#555; stroke-width:1.5; }}
.water {{ fill:#7ec8e3; opacity:.75; }}
.terrain {{ fill:none; stroke:#333; stroke-width:2; }}
.invert {{ fill:none; stroke:#666; stroke-width:1.2; stroke-dasharray:5 3; }}
.well {{ stroke:#444; stroke-width:1.2; }}
.nodeNormal {{ fill:#fff; stroke:#245b9c; stroke-width:1.6; }}
.nodeFlood {{ fill:#d7191c; stroke:#8b0000; stroke-width:2.0; }}
.floodLabel {{ font-size:11px; fill:#b00000; font-weight:bold; }}
.nodeLabel {{ font-size:10px; fill:#245b9c; }}
</style>
</head>
<body>
<div id=\"wrap\">
<h2>Dynamic SWMM profiles from .OUT file</h2>
<div class=\"controls\">
<label>Pipe: <select id=\"branchSel\"></select></label>
<label>Timestep: <input id=\"timeSlider\" type=\"range\" min=\"0\" max=\"0\" value=\"0\"></label>
<button id=\"playBtn\">▶ Play</button>
<button id=\"critBtn\">Go to critical timestep</button>
<span id=\"timeInfo\" class=\"badge\"></span>
</div>
<svg id=\"svg\" viewBox=\"0 0 1200 610\"></svg>
<p class=\"note\">The profile uses time series read from the .OUT file: Q, water depth, velocity and filling for each timestep. The critical timestep is the one where the maximum flow is observed among the conduits of the selected pipe path.</p>
</div>
<script>
const DATA = {js_payload};
let currentBranch = 0;
let timer = null;
const branchSel = document.getElementById('branchSel');
const slider = document.getElementById('timeSlider');
const timeInfo = document.getElementById('timeInfo');
const svg = document.getElementById('svg');
const playBtn = document.getElementById('playBtn');
const critBtn = document.getElementById('critBtn');

function esc(s) {{ return String(s ?? '').replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c])); }}
function num(v, d=2) {{ return (v === null || v === undefined || Number.isNaN(Number(v))) ? '-' : Number(v).toFixed(d); }}
function colorPct(p) {{
  if (p === null || p === undefined || Number.isNaN(Number(p))) return '#4a90e2';
  p = Number(p);
  if (p < 50) return '#2c7bb6';
  if (p < 80) return '#1a9641';
  if (p <= 100) return '#fdae61';
  return '#d7191c';
}}
function init() {{
  DATA.branches.forEach((b,i)=>{{ const o=document.createElement('option'); o.value=i; o.textContent=b.title; branchSel.appendChild(o); }});
  slider.max = Math.max(0, (DATA.times || []).length - 1);
  branchSel.onchange = () => {{ currentBranch = Number(branchSel.value); draw(Number(slider.value)); }};
  slider.oninput = () => draw(Number(slider.value));
  playBtn.onclick = togglePlay;
  critBtn.onclick = () => {{ const b=DATA.branches[currentBranch]; slider.value = b.critical_index || 0; draw(Number(slider.value)); }};
  draw(0);
}}
function togglePlay() {{
  if (timer) {{ clearInterval(timer); timer=null; playBtn.textContent='▶ Play'; return; }}
  playBtn.textContent='⏸ Pausa';
  timer = setInterval(()=>{{ let i=Number(slider.value)+1; if (i>Number(slider.max)) i=0; slider.value=i; draw(i); }}, 650);
}}
function draw(ti) {{
  const b = DATA.branches[currentBranch];
  if (!b) return;
  const W=1200, H=610, ml=65, mr=35, mt=50, mb=65;
  const xs = b.x || [];
  let ys=[];
  (b.nodes || []).forEach(n=>{{ if(n.ground!==null) ys.push(Number(n.ground)); if(n.invert!==null) ys.push(Number(n.invert)); }});
  (b.links || []).forEach(l=>{{ if(l.z0!==null) {{ ys.push(Number(l.z0)); ys.push(Number(l.z0)+Number(l.diam||0.3)); }} if(l.z1!==null) {{ ys.push(Number(l.z1)); ys.push(Number(l.z1)+Number(l.diam||0.3)); }} }});
  if (!ys.length) ys=[0,1];
  const xmin = Math.min(...xs, 0), xmax = Math.max(...xs, 1);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  const pad = Math.max(0.5, (ymax-ymin)*0.18); ymin -= pad; ymax += pad;
  function X(x) {{ return ml + (Number(x)-xmin)/(xmax-xmin || 1)*(W-ml-mr); }}
  function Y(y) {{ return mt + (ymax-Number(y))/(ymax-ymin || 1)*(H-mt-mb); }}
  let out = '';
  out += `<rect x="0" y="0" width="${{W}}" height="${{H}}" fill="#fbfbfb"/>`;
  for (let k=0;k<6;k++) {{ const y=ymin+(ymax-ymin)*k/5; out += `<line class="grid" x1="${{ml}}" y1="${{Y(y)}}" x2="${{W-mr}}" y2="${{Y(y)}}"/><text class="small" x="8" y="${{Y(y)+4}}">${{num(y,2)}}</text>`; }}
  const terrainPts = (b.nodes || []).map((n,i)=> n.ground===null ? null : `${{X(xs[i])}},${{Y(n.ground)}}`).filter(Boolean).join(' ');
  const invertPts = (b.nodes || []).map((n,i)=> n.invert===null ? null : `${{X(xs[i])}},${{Y(n.invert)}}`).filter(Boolean).join(' ');
  if (terrainPts) out += `<polyline class="terrain" points="${{terrainPts}}"/>`;
  if (invertPts) out += `<polyline class="invert" points="${{invertPts}}"/>`;
  let floodedNodes = [];
  (b.nodes || []).forEach((n,i)=>{{
    const x = X(xs[i]);
    const gy = Y(n.ground ?? n.invert ?? ymin);
    if(n.ground!==null && n.invert!==null) out += `<line class="well" x1="${{x}}" y1="${{Y(n.ground)}}" x2="${{x}}" y2="${{Y(n.invert)}}"/>`;
    const flood = (n.flooding || [])[ti];
    const dep = (n.depth || [])[ti];
    const head = (n.head || [])[ti];
    const isFlood = flood !== null && flood !== undefined && !Number.isNaN(Number(flood)) && Math.abs(Number(flood)) > 1e-9;
    out += `<circle class="${{isFlood ? 'nodeFlood' : 'nodeNormal'}}" cx="${{x}}" cy="${{gy-18}}" r="6"><title>${{esc(n.id)}}\nDepth=${{num(dep,3)}} m\nHead=${{num(head,3)}} m\nFlooding=${{num(flood,3)}} L/s</title></circle>`;
    out += `<text class="label" text-anchor="middle" x="${{x}}" y="${{gy-28}}">${{esc(n.id)}}</text>`;
    out += `<text class="nodeLabel" text-anchor="middle" x="${{x}}" y="${{gy+14}}">d=${{num(dep,2)}} m</text>`;
    if (isFlood) {{
      floodedNodes.push(`${{esc(n.id)}}=${{num(flood,2)}} L/s`);
      out += `<text class="floodLabel" text-anchor="middle" x="${{x}}" y="${{gy-42}}">Flood ${{num(flood,2)}} L/s</text>`;
    }}
  }});
  (b.links || []).forEach((l,i)=>{{
    const x0=xs[i], x1=xs[i+1], z0=Number(l.z0), z1=Number(l.z1), d=Number(l.diam || 0.3);
    const q = (l.q || [])[ti], h = (l.h || [])[ti], v = (l.v || [])[ti];
    const typ = String(l.type || 'CONDUIT').toUpperCase();
    let pct = null;
    if (h !== null && h !== undefined && d>0) pct = Number(h)/d*100.0;
    else if ((l.capacity || [])[ti] !== undefined && (l.capacity || [])[ti] !== null) pct = Number((l.capacity || [])[ti])*100.0;
    const hp = Math.max(0, Math.min(Number(h || 0), d));
    const col = colorPct(pct);
    const sx0=X(x0), sx1=X(x1), sy0=Y(z0), sy1=Y(z1);
    if (typ === 'CONDUIT') {{
      out += `<polygon class="pipe" points="${{sx0}},${{sy0}} ${{sx1}},${{sy1}} ${{X(x1)}},${{Y(z1+d)}} ${{X(x0)}},${{Y(z0+d)}}" style="stroke:${{col}}"/>`;
      if (hp>0) out += `<polygon class="water" points="${{X(x0)}},${{Y(z0)}} ${{X(x1)}},${{Y(z1)}} ${{X(x1)}},${{Y(z1+hp)}} ${{X(x0)}},${{Y(z0+hp)}}"/>`;
    }} else {{
      const xm0=(sx0+sx1)/2, ym0=(sy0+sy1)/2;
      const regColor = typ === 'PUMP' ? '#7b3294' : (typ === 'WEIR' ? '#1b9e77' : '#d95f02');
      const sym = typ === 'PUMP' ? 'P' : (typ === 'WEIR' ? 'W' : 'O');
      out += `<line x1="${{sx0}}" y1="${{sy0}}" x2="${{sx1}}" y2="${{sy1}}" style="stroke:${{regColor}};stroke-width:2.2"/>`;
      out += `<circle cx="${{xm0}}" cy="${{ym0}}" r="13" style="fill:white;stroke:${{regColor}};stroke-width:2.2"><title>${{esc(l.id)}} - ${{typ}}\nQ=${{num(q,2)}} L/s</title></circle>`;
      out += `<text class="label" text-anchor="middle" x="${{xm0}}" y="${{ym0+4}}" style="fill:${{regColor}};font-weight:bold">${{sym}}</text>`;
    }}
    const xm=(X(x0)+X(x1))/2, ym=(Y(z0+d)+Y(z1+d))/2 - 8;
    out += `<text class="small" text-anchor="middle" x="${{xm}}" y="${{ym}}">${{esc(l.id)}} | ${{typ}} | Q=${{num(q,1)}} L/s | h%=${{num(pct,0)}} | v=${{num(v,2)}} m/s</text>`;
  }});
  out += `<circle class="nodeNormal" cx="${{W-250}}" cy="24" r="6"/><text class="small" x="${{W-238}}" y="28">Nodo senza esondazione</text>`;
  out += `<circle class="nodeFlood" cx="${{W-250}}" cy="43" r="6"/><text class="small" x="${{W-238}}" y="47">Nodo con esondazione</text>`;
  if (floodedNodes.length) out += `<text class="floodLabel" x="${{ml}}" y="45">Esondazione nodi: ${{floodedNodes.join(' | ')}}</text>`;
  out += `<text class="label" x="${{W/2}}" y="24" text-anchor="middle">${{esc(b.title)}} - ${{esc(DATA.times[ti] || '')}}</text>`;
  out += `<text class="small" x="${{ml}}" y="${{H-18}}">Distanza progressiva [m]</text>`;
  svg.innerHTML = out;
  timeInfo.textContent = `${{ti+1}} / ${{DATA.times.length}} - ${{DATA.times[ti] || ''}}`;
}}
init();
</script>
</body>
</html>"""
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(html)
        return out_html

    def create_swmm_dynamic_profiles_from_out(self, cond_layer, nodi_layer, out_file, outdir, orifice_defs=None, pump_defs=None, weir_defs=None):
        """Create a dynamic HTML profile viewer by reading time series from the .OUT file.

        Static plots with maximum results are kept separate, and a new
        dynamic_swmm_profiles folder is added containing:
        - dynamic_swmm_out_viewer.html
        - link_time_series.csv
        - node_time_series.csv
        """
        try:
            import csv
        except Exception:
            csv = None

        if not out_file or not os.path.exists(out_file):
            self.log_msg('Warning: .OUT file not found; dynamic profiles cannot be created.')
            return None

        dynamic_dir = os.path.join(outdir, "dynamic_swmm_profiles")
        os.makedirs(dynamic_dir, exist_ok=True)

        nodes = {}
        for f in nodi_layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            nid = self._node_id_from_feature(f)
            try:
                pt = g.asPoint()
                x_pt, y_pt = pt.x(), pt.y()
            except Exception:
                x_pt, y_pt = None, None
            ground = self.attr_float(f, ["elevaz", "ELEVAZ", "quota_terr", "ground_elevation", "ELEVATION", "q_terr"], None)
            invert = self.attr_float(f, ["q_scorr", "Q_SCORR", "quota_fondo", "invert", "invert_elevation", "q_fondo"], None)
            prof = self.attr_float(f, ["prof_scav", "PROF_SCAV", "excavation_depth", "max_depth", "depth"], None)
            if ground is None and invert is not None and prof is not None:
                ground = invert + prof
            nodes[nid] = {"ground": ground, "invert": invert, "x": x_pt, "y": y_pt}

        conduits = []
        for f in cond_layer.getFeatures():
            cid = self._conduit_id_from_feature(f)
            fn = self.attr(f, ["id_monte", "ID_MONTE", "from_node", "FROM_NODE", "Da", "nodo_monte", "from"])
            tn = self.attr(f, ["id_valle", "ID_VALLE", "to_node", "TO_NODE", "A", "nodo_valle", "to"])
            if fn is None or tn is None:
                continue
            fn = str(fn); tn = str(tn)
            length = self.attr_float(f, ["Lenght", "LENGHT", "length", "Length", "LENGTH", "lunghezza"], None)
            geom = f.geometry()
            if (length is None or length <= 0) and geom and not geom.isEmpty():
                length = geom.length()
            length = max(float(length or 0.0), 0.0)
            diam_mm = self.attr_float(f, ["diam_mm", "DN", "diametro", "D_mm"], None)
            diam_m = self.attr_float(f, ["D", "diam_m"], None)
            if diam_m is None:
                diam_m = (diam_mm / 1000.0) if diam_mm else None
            z_monte = self.attr_float(f, ["z_monte", "Z_MONTE", "scorr_monte", "q_monte"], None)
            z_valle = self.attr_float(f, ["z_valle", "Z_VALLE", "scorr_valle", "q_valle"], None)
            if z_monte is None:
                z_monte = nodes.get(fn, {}).get("invert")
            if z_valle is None:
                z_valle = nodes.get(tn, {}).get("invert")
            conduits.append({
                "cond_id": cid,
                "from_node": fn,
                "to_node": tn,
                "length": length,
                "diam_m": float(diam_m) if diam_m not in (None, 0) else 0.30,
                "z_monte": z_monte,
                "z_valle": z_valle,
            })

        conduits = self._merge_profile_regulator_links(conduits, nodes, orifice_defs, pump_defs, weir_defs)
        paths = self._build_profile_paths(conduits)
        if not paths:
            self.log_msg('Warning: no valid path for dynamic profiles.')
            return None

        link_ids = [c["cond_id"] for c in conduits]
        node_ids = list(nodes.keys())
        ts = self._read_out_timeseries_with_pyswmm(out_file, link_ids, node_ids)
        times = ts.get("times") or []
        if not times:
            self.log_msg('Warning: the .OUT file does not contain readable timesteps for dynamic profiles.')
            return None

        # Summary CSV of link time series, useful for external checks.
        if csv is not None:
            csv_path = os.path.join(dynamic_dir, "link_time_series.csv")
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["time", "link_id", "q_lps", "h_m", "v_ms", "capacity"])
                for lid, rec in ts.get("links", {}).items():
                    q = rec.get("q") or []
                    h = rec.get("h") or []
                    v = rec.get("v") or []
                    cap = rec.get("capacity") or []
                    for i, tm in enumerate(times):
                        w.writerow([tm, lid, q[i] if i < len(q) else None, h[i] if i < len(h) else None, v[i] if i < len(v) else None, cap[i] if i < len(cap) else None])
            self.log_msg(f"CSV serie temporali link creato: {csv_path}")

            node_csv_path = os.path.join(dynamic_dir, "node_time_series.csv")
            with open(node_csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["time", "node_id", "depth_m", "head_m", "flooding_lps"])
                for nid, rec in ts.get("nodes", {}).items():
                    depth = rec.get("depth") or []
                    head = rec.get("head") or []
                    flooding = rec.get("flooding") or []
                    for i, tm in enumerate(times):
                        w.writerow([
                            tm,
                            nid,
                            depth[i] if i < len(depth) else None,
                            head[i] if i < len(head) else None,
                            flooding[i] if i < len(flooding) else None,
                        ])
            self.log_msg(f"CSV serie temporali nodi creato: {node_csv_path}")

        branches = []
        for idx, path in enumerate(paths, start=1):
            if not path:
                continue
            x_vals = [0.0]
            node_seq = [path[0]["from_node"]]
            for c in path:
                x_vals.append(x_vals[-1] + float(c.get("length") or 0.0))
                node_seq.append(c.get("to_node"))

            links_payload = []
            branch_qmax = -1e99
            branch_crit_idx = 0
            for c in path:
                cid = c.get("cond_id")
                rec = ts.get("links", {}).get(cid, {})
                q = rec.get("q") or []
                h = rec.get("h") or []
                v = rec.get("v") or []
                cap = rec.get("capacity") or []
                for i, val in enumerate(q):
                    if val is not None and float(val) > branch_qmax:
                        branch_qmax = float(val)
                        branch_crit_idx = i
                links_payload.append({
                    "id": cid,
                    "type": c.get("link_type", "CONDUIT"),
                    "label": c.get("regulator_label", ""),
                    "from": c.get("from_node"),
                    "to": c.get("to_node"),
                    "z0": c.get("z_monte"),
                    "z1": c.get("z_valle"),
                    "diam": c.get("diam_m") or 0.30,
                    "q": q,
                    "h": h,
                    "v": v,
                    "capacity": cap,
                })

            nodes_payload = []
            for nid in node_seq:
                nd = nodes.get(nid, {})
                nrec = ts.get("nodes", {}).get(nid, {})
                nodes_payload.append({
                    "id": nid,
                    "ground": nd.get("ground"),
                    "invert": nd.get("invert"),
                    "depth": nrec.get("depth") or [],
                    "head": nrec.get("head") or [],
                    "flooding": nrec.get("flooding") or [],
                })

            start = node_seq[0]
            end = node_seq[-1]
            title = f"Dynamic profile {idx}: {start} → {end}"
            branches.append({
                "title": title,
                "x": x_vals,
                "nodes": nodes_payload,
                "links": links_payload,
                "critical_index": int(branch_crit_idx),
            })

        payload = {"times": times, "branches": branches}
        html = self._write_dynamic_swmm_profile_html(dynamic_dir, payload)
        self.log_msg(f"Dynamic SWMM viewer created from .OUT: {html}")
        return html
    def load_swmm_results_to_project(self, cond_layer, nodi_layer, rpt_file, outdir, out_file=None, orifice_defs=None, pump_defs=None, weir_defs=None):
        link_results, node_results = self.parse_swmm_rpt_results(rpt_file)
        flooded_count = sum(1 for r in node_results.values() if (r.get("flood_m3") or 0) > 0)
        self.log_msg(f"Results read from RPT: {len(link_results)} condotte con massimi, {len(node_results)} nodi con massimi, {flooded_count} nodi flooded.")

        # Conduit layer with Qmax and Vmax
        cond_res_layer, cond_extra = self.make_memory_layer_like(
            cond_layer,
            "condotte_swmm_risultati",
            [("qmax_lps", QVariant.Double), ("vmax_ms", QVariant.Double), ("hmax_m", QVariant.Double), ("hmax_pct", QVariant.Double)]
        )
        cpr = cond_res_layer.dataProvider()
        q_field, v_field, h_field, hpct_field = cond_extra
        for f in cond_layer.getFeatures():
            cond_id = self.attr(f, ["cond_id", "COND_ID", "link_id", "id", "Id", "ID", "nome"])
            cond_id = str(cond_id) if cond_id is not None else f"COND_{f.id()}"
            res = link_results.get(cond_id, {})
            diam_mm = self.attr_float(f, ["diam_mm", "DN", "diametro", "D_mm"], None)
            diam_m = self.attr_float(f, ["D", "diam_m"], None)
            if diam_m is None:
                diam_m = (diam_mm / 1000.0) if diam_mm else None
            max_full_depth = res.get("max_full_depth")
            hmax_m = res.get("hmax_m")
            # If the report does not contain the absolute water depth, use only as fallback
            # the Max/Full Depth ratio multiplied by the diameter.
            if hmax_m is None and max_full_depth is not None and diam_m is not None:
                hmax_m = max_full_depth * diam_m
            if hmax_m is not None and diam_m not in (None, 0):
                hmax_pct = (hmax_m / diam_m) * 100.0
            else:
                hmax_pct = (max_full_depth * 100.0) if max_full_depth is not None else None
            nf = QgsFeature(cond_res_layer.fields())
            nf.setGeometry(f.geometry())
            attrs = list(f.attributes()) + [res.get("qmax_lps"), res.get("vmax_ms"), hmax_m, hmax_pct]
            nf.setAttributes(attrs)
            cpr.addFeature(nf)
        cond_res_layer.updateExtents()
        self.apply_condotte_results_style(cond_res_layer)
        QgsProject.instance().addMapLayer(cond_res_layer)
        cond_path = os.path.join(outdir, "condotte_swmm_risultati.gpkg")
        QgsVectorFileWriter.writeAsVectorFormat(cond_res_layer, cond_path, "UTF-8", cond_res_layer.crs(), "GPKG")
        self.log_msg(f"Conduit results layer loaded and saved: {cond_path}")

        # Node layer with flooding results. Nodes missing from Node Flooding Summary have zero volume.
        nodi_res_layer, nodi_extra = self.make_memory_layer_like(
            nodi_layer,
            "nodi_swmm_risultati",
            [
                ("max_dep_m", QVariant.Double),
                ("max_hgl_m", QVariant.Double),
                ("avg_dep_m", QVariant.Double),
                ("flood_h", QVariant.Double),
                ("flood_lps", QVariant.Double),
                ("flood_m3", QVariant.Double),
                ("flood_10e6l", QVariant.Double),
            ]
        )
        npr = nodi_res_layer.dataProvider()
        for f in nodi_layer.getFeatures():
            node_id = self.attr(f, ["node_id", "NODE_ID", "id", "Id", "ID", "nome", "Name"])
            node_id = str(node_id) if node_id is not None else f"N{f.id()}"
            res = node_results.get(node_id, {})
            nf = QgsFeature(nodi_res_layer.fields())
            nf.setGeometry(f.geometry())
            attrs = list(f.attributes()) + [
                res.get("max_depth_m"),
                res.get("max_hgl_m"),
                res.get("avg_depth_m"),
                res.get("flood_h", 0.0),
                res.get("flood_lps", 0.0),
                res.get("flood_m3", 0.0),
                res.get("flood_10e6l", 0.0),
            ]
            nf.setAttributes(attrs)
            npr.addFeature(nf)
        nodi_res_layer.updateExtents()
        self.apply_nodi_results_style(nodi_res_layer)
        QgsProject.instance().addMapLayer(nodi_res_layer)
        nodi_path = os.path.join(outdir, "nodi_swmm_risultati.gpkg")
        QgsVectorFileWriter.writeAsVectorFormat(nodi_res_layer, nodi_path, "UTF-8", nodi_res_layer.crs(), "GPKG")
        self.log_msg(f"Node results layer loaded and saved: {nodi_path}")

        # Also create PNG images of longitudinal profiles in SWMM style,
        # using Qmax_lps, hmax_m and hmax_pct read from or derived from the report.
        try:
            self.create_swmm_profile_images(cond_layer, nodi_layer, link_results, outdir, out_file, orifice_defs, pump_defs, weir_defs)
        except Exception as e:
            self.log_msg(f"Avviso: impossibile creare le immagini dei profili SWMM: {e}")

        # Also create the timestep-by-timestep dynamic profile by reading the .OUT file.
        # Requires PySWMM installed in the QGIS Python environment. If unavailable,
        # the simulation and maximum results remain available.
        try:
            self.create_swmm_dynamic_profiles_from_out(cond_layer, nodi_layer, out_file, outdir, orifice_defs, pump_defs, weir_defs)
        except Exception as e:
            self.log_msg(f"Avviso: impossibile creare il profilo dinamico da .OUT: {e}")


class SewerSWMMBuilderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(QIcon(icon_path), "SWMM Sewer Builder", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        try:
            self.iface.mainWindow().setIconSize(QSize(32, 32))
        except Exception:
            pass
        self.iface.addPluginToMenu("&SWMM Sewer Builder", self.action)

    def unload(self):
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&SWMM Sewer Builder", self.action)

    def run(self):
        self.dialog = SewerSWMMBuilderDialog(self.iface, self.plugin_dir, self.iface.mainWindow())
        self.dialog.show()

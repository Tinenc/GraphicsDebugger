#include "MeshUVView.h"
#include "Code/QRDUtils.h"
#include "TexturePreviewChannels.h"

#include <QApplication>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QOpenGLContext>
#include <QOpenGLFramebufferObject>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QPainter>
#include <QWheelEvent>
#include <QtMath>
#include <cmath>
#include <limits>

QImage MeshUVTextureView::snapshot()
{
  if(!isValid() || !context())
    return QImage();
  makeCurrent();
  // Render geometry again at evidence resolution. Enlarging a small dock's screen
  // pixels makes the feature unreadable even when the captured geometry is complete.
  const QSize native = size() * devicePixelRatio();
  const qreal scale = qMax(1.0, qMin(4.0, 2048.0 / qMax(1, qMax(native.width(), native.height()))));
  QImage image;
  {
    QOpenGLFramebufferObject buffer(native * scale, QOpenGLFramebufferObject::CombinedDepthStencil);
    if(buffer.isValid() && buffer.bind())
    {
      m_SnapshotSize = buffer.size();
      glViewport(0, 0, m_SnapshotSize.width(), m_SnapshotSize.height());
      paintGL();
      image = buffer.toImage();
      m_SnapshotSize = QSize();
    }
    glBindFramebuffer(GL_FRAMEBUFFER, defaultFramebufferObject());
    glViewport(0, 0, native.width(), native.height());
  }
  doneCurrent();
  return image;
}

namespace
{
const QColor SelectionColor(255, 155, 52);

struct MeshPointSprite
{
  QVector4D position;
  QVector2D corner;
};

void appendPointSprite(QVector<MeshPointSprite> &vertices, const QVector4D &position)
{
  const QVector2D corners[] = {{-1, -1}, {1, -1}, {-1, 1}, {-1, 1}, {1, -1}, {1, 1}};
  for(const QVector2D &corner : corners)
    vertices.push_back({position, corner});
}

double cross(const QPointF &a, const QPointF &b)
{
  return a.x() * b.y() - a.y() * b.x();
}

double segmentDistance(const QPointF &p, const QPointF &a, const QPointF &b)
{
  const QPointF delta = b - a;
  const double length = QPointF::dotProduct(delta, delta);
  const double t = length > 0.0 ? qBound(0.0, QPointF::dotProduct(p - a, delta) / length, 1.0) : 0.0;
  return QLineF(p, a + t * delta).length();
}

bool barycentric(const QPointF &p, const QPointF &a, const QPointF &b, const QPointF &c, double &u,
                 double &v, double &w)
{
  const double area = cross(b - a, c - a);
  if(std::abs(area) < 1e-9)
    return false;
  u = cross(b - p, c - p) / area;
  v = cross(c - p, a - p) / area;
  w = 1.0 - u - v;
  return u >= 0 && v >= 0 && w >= 0;
}

// Clip against the two depth planes before the perspective divide. The visible portion of a
// triangle or edge can still be picked when one of its vertices is behind the camera.
bool meshClipVisible(const QVector4D &p)
{
  return p.w() > 0.0f && p.z() >= 0.0f && p.z() <= p.w();
}

float meshClipDistance(const QVector4D &p, bool nearPlane)
{
  return nearPlane ? p.z() : p.w() - p.z();
}

bool clipMeshEdge(QVector4D &a, QVector4D &b)
{
  for(bool nearPlane : {true, false})
  {
    const float da = meshClipDistance(a, nearPlane), db = meshClipDistance(b, nearPlane);
    if(da < 0 && db < 0)
      return false;
    if((da < 0) != (db < 0))
    {
      const QVector4D intersection = a + (b - a) * (da / (da - db));
      (da < 0 ? a : b) = intersection;
    }
  }
  return a.w() > 0.0f && b.w() > 0.0f;
}

struct ProjectedMesh
{
  struct Polygon
  {
    std::array<QVector4D, 5> vertices;
    int count = 0;
  };

  ProjectedMesh(const MeshUVGeometry &geometry, const QSize &size, const QMatrix4x4 &transform,
                bool homogenous,
                const std::function<QVector4D(const MeshUVGeometry::Vertex &)> &meshPosition = {})
      : viewport(size)
  {
    vertices.reserve(geometry.vertices.size());
    for(const auto &vertex : geometry.vertices)
    {
      QVector4D position = meshPosition ? meshPosition(vertex) : vertex.position;
      if(!homogenous)
        position.setW(1.0f);
      vertices.push_back(transform * position);
    }
  }

  QPointF point(const QVector4D &p) const
  {
    const double invW = 1.0 / p.w();
    return QPointF((p.x() * invW + 1.0) * viewport.width() * .5,
                   (1.0 - p.y() * invW) * viewport.height() * .5);
  }

  Polygon face(const std::array<int, 3> &rows) const
  {
    Polygon polygon = {{{vertices[rows[0]], vertices[rows[1]], vertices[rows[2]], {}, {}}}, 3};
    if(meshClipVisible(polygon.vertices[0]) && meshClipVisible(polygon.vertices[1]) &&
       meshClipVisible(polygon.vertices[2]))
      return polygon;

    for(bool nearPlane : {true, false})
    {
      if(polygon.count < 3)
        break;
      Polygon output;
      QVector4D a = polygon.vertices[polygon.count - 1];
      float da = meshClipDistance(a, nearPlane);
      for(int i = 0; i < polygon.count; ++i)
      {
        const QVector4D &b = polygon.vertices[i];
        const float db = meshClipDistance(b, nearPlane);
        if((da < 0) != (db < 0))
          output.vertices[output.count++] = a + (b - a) * (da / (da - db));
        if(db >= 0)
          output.vertices[output.count++] = b;
        a = b;
        da = db;
      }
      polygon = output;
    }
    for(int i = 0; i < polygon.count; ++i)
      if(!(polygon.vertices[i].w() > 0.0f))
        return {};
    return polygon;
  }

  bool component(const std::array<int, 3> &rows, int corner, MeshUVData::Selection mode, QPointF &a,
                 QPointF &b) const
  {
    QVector4D first = vertices[rows[corner]], second = vertices[rows[(corner + 1) % 3]];
    if(mode == MeshUVData::Selection::Point)
    {
      if(!meshClipVisible(first))
        return false;
      a = point(first);
      return true;
    }
    if(!clipMeshEdge(first, second))
      return false;
    a = point(first);
    b = point(second);
    return true;
  }

  QSize viewport;
  QVector<QVector4D> vertices;
};
}

MeshCameraGesture meshCameraGesture(int preset, Qt::MouseButtons buttons, Qt::KeyboardModifiers mods)
{
  const bool alt = (mods & Qt::AltModifier) != 0;
  if(preset == 1)    // 3ds Max
  {
    if(buttons != Qt::MiddleButton)
      return MeshCameraGesture::None;
    if(alt && (mods & Qt::ControlModifier))
      return MeshCameraGesture::Dolly;
    return alt ? MeshCameraGesture::Orbit : MeshCameraGesture::Pan;
  }
  if(preset == 2)    // Maya
  {
    if(!alt)
      return MeshCameraGesture::None;
    if(buttons == Qt::LeftButton)
      return MeshCameraGesture::Orbit;
    if(buttons == Qt::MiddleButton)
      return MeshCameraGesture::Pan;
    if(buttons == Qt::RightButton || buttons == (Qt::LeftButton | Qt::MiddleButton))
      return MeshCameraGesture::Dolly;
    return MeshCameraGesture::None;
  }
  if(buttons == Qt::MiddleButton || (alt && buttons == Qt::LeftButton))
    return MeshCameraGesture::Pan;
  return buttons == Qt::LeftButton ? MeshCameraGesture::Orbit : MeshCameraGesture::None;
}

quint64 MeshUVData::key(int triangle, int corner) const
{
  if(mode == Selection::Face)
    return quint64(triangle);
  const auto &face = geometry->triangles[triangle];
  const uint32_t a = geometry->vertices[face[corner]].index;
  if(mode == Selection::Point)
    return a;
  const uint32_t b = geometry->vertices[face[(corner + 1) % 3]].index;
  return (quint64(qMin(a, b)) << 32) | qMax(a, b);
}

QSet<uint32_t> MeshUVData::selectedVertices() const
{
  QSet<uint32_t> vertices;
  if(!geometry)
    return vertices;
  if(mode == Selection::Point)
  {
    for(quint64 key : selected)
      vertices.insert(uint32_t(key));
    return vertices;
  }
  const int count = mode == Selection::Face ? 3 : 2;
  for(int face = 0; face < geometry->triangles.size(); ++face)
    for(int corner = 0; corner < elementCount(); ++corner)
      if(selected.contains(key(face, corner)))
        for(int i = 0; i < count; ++i)
          vertices.insert(geometry->vertices[geometry->triangles[face][(corner + i) % 3]].index);
  return vertices;
}

void MeshUVData::selectVertices(const QSet<uint32_t> &vertices)
{
  selected.clear();
  if(!geometry)
    return;
  if(mode == Selection::Point)
  {
    for(const auto &vertex : geometry->vertices)
      if(vertices.contains(vertex.index))
        selected.insert(vertex.index);
    return;
  }
  for(int face = 0; face < geometry->triangles.size(); ++face)
    for(int corner = 0; corner < elementCount(); ++corner)
    {
      const int count = mode == Selection::Face ? 3 : mode == Selection::Edge ? 2 : 1;
      bool complete = true;
      for(int i = 0; i < count; ++i)
        complete &=
            vertices.contains(geometry->vertices[geometry->triangles[face][(corner + i) % 3]].index);
      if(complete)
        selected.insert(key(face, corner));
    }
}

void MeshUVData::changeMode(Selection next)
{
  if(mode == next)
    return;
  const QSet<uint32_t> vertices = selectedVertices();
  if(!geometry || mode == Selection::Point || next == Selection::Point)
  {
    mode = next;
    selectVertices(vertices);
    return;
  }
  // Convert source edges before changing modes, so shared vertices do not expand the selection.
  QSet<quint64> converted;
  for(int face = 0; face < geometry->triangles.size(); ++face)
  {
    const auto &triangle = geometry->triangles[face];
    const auto edgeKey = [&](int corner) {
      const uint32_t a = geometry->vertices[triangle[corner]].index;
      const uint32_t b = geometry->vertices[triangle[(corner + 1) % 3]].index;
      return (quint64(qMin(a, b)) << 32) | qMax(a, b);
    };
    if(mode == Selection::Face && selected.contains(quint64(face)))
      for(int corner = 0; corner < 3; ++corner)
        converted.insert(edgeKey(corner));
    else if(mode == Selection::Edge && selected.contains(edgeKey(0)) &&
            selected.contains(edgeKey(1)) && selected.contains(edgeKey(2)))
      converted.insert(quint64(face));
  }
  mode = next;
  selected = std::move(converted);
}

void MeshUVView::setSelectionMode(MeshUVData::Selection mode)
{
  m_Mesh->changeMode(mode);
  notifySelection();
}

void MeshUVView::selectVertices(const QSet<uint32_t> &vertices)
{
  m_Mesh->selectVertices(vertices);
  notifySelection();
}

void MeshUVView::selectElements(const QSet<quint64> &elements)
{
  m_Mesh->selected.clear();
  if(m_Mesh->geometry)
    for(int face = 0; face < m_Mesh->geometry->triangles.size(); ++face)
      for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      {
        const quint64 key = m_Mesh->key(face, corner);
        if(elements.contains(key))
          m_Mesh->selected.insert(key);
      }
  notifySelection();
}

MeshUVView::MeshUVView(std::shared_ptr<MeshUVData> mesh, QWidget *parent)
    : QWidget(parent), m_Mesh(mesh)
{
  setFocusPolicy(Qt::StrongFocus);
  setMinimumSize(140, 100);
  setAttribute(Qt::WA_OpaquePaintEvent);
}

void MeshUVView::resetView()
{
  m_Pan = {};
  m_Zoom = 1.0;
  setProperty("uvZoom", m_Zoom);
  update();
}

void MeshUVView::geometryChanged()
{
  m_Wire = QPixmap();
  m_WireDensity = 0.0;
  setProperty("uvTriangleCount", m_Mesh->geometry ? m_Mesh->geometry->triangles.size() : 0);
  clearSelection();
}

QImage MeshUVData::displayTexture() const
{
  if(m_DisplaySourceKey != texture.cacheKey() || m_DisplayChannels != textureChannels)
  {
    m_DisplaySourceKey = texture.cacheKey();
    m_DisplayChannels = textureChannels;
    m_DisplayTexture = TexturePreviewChannels(texture, textureChannels);
  }
  return m_DisplayTexture;
}

void MeshUVView::textureChanged()
{
  m_Texture = QPixmap::fromImage(m_Mesh->displayTexture().mirrored(false, !m_Mesh->flipTexture));
  setProperty("uvTextureReady", !m_Texture.isNull());
  update();
}

void MeshUVView::focusSelection()
{
  QRectF bounds = m_SelectedFaces.boundingRect();
  for(const QLineF &edge : m_SelectedEdges)
    bounds |= QRectF(edge.p1(), edge.p2()).normalized().adjusted(-.001, -.001, .001, .001);
  for(const QPointF &p : m_SelectedPoints)
    bounds |= QRectF(p - QPointF(.001, .001), QSizeF(.002, .002));
  if(bounds.isEmpty() && m_Mesh->geometry)
    bounds = m_Mesh->geometry->bounds;
  if(bounds.isEmpty())
  {
    resetView();
    return;
  }
  m_Zoom = qBound(.01, .85 / qMax(.02, qMax(bounds.width(), bounds.height())), 40.0);
  m_Pan = QPointF(.5 - bounds.center().x(), bounds.center().y() - .5);
  setProperty("uvZoom", m_Zoom);
  update();
}

QTransform MeshUVView::atlasTransform() const
{
  const double side = qMax(1, qMin(width(), height()) - 24) * m_Zoom;
  const QPointF origin = QRectF(rect()).center() + (m_Pan + QPointF(-.5, .5)) * side;
  return QTransform(side, 0, 0, -side, origin.x(), origin.y());
}

void MeshUVView::frameOverview()
{
  QRectF bounds(0, 0, 1, 1);
  if(m_Mesh->geometry)
    bounds |= m_Mesh->geometry->bounds;
  m_Zoom = .85 / qMax(.02, qMax(bounds.width(), bounds.height()));
  m_Pan = QPointF(.5 - bounds.center().x(), bounds.center().y() - .5);
  setProperty("uvZoom", m_Zoom);
  update();
}

QImage MeshUVView::snapshot()
{
  m_SnapshotScale = qMax(1.0, qMin(4.0, 2048.0 / qMax(1, qMax(width(), height()))));
  QImage image(size() * m_SnapshotScale, QImage::Format_RGB32);
  image.setDevicePixelRatio(m_SnapshotScale);
  render(&image);
  image.setDevicePixelRatio(1.0);
  m_SnapshotScale = 1.0;
  return image;
}

QRectF MeshUVView::overviewImageBounds() const
{
  QRectF bounds(0, 0, 1, 1);
  if(m_Mesh->geometry)
    bounds |= m_Mesh->geometry->bounds;
  bounds = atlasTransform().mapRect(bounds).adjusted(-16, -20, 16, 20).intersected(rect());
  return QRectF(bounds.x() / qMax(1, width()), bounds.y() / qMax(1, height()),
                bounds.width() / qMax(1, width()), bounds.height() / qMax(1, height()));
}

QRectF MeshUVView::selectionImageBounds() const
{
  if(!m_Mesh->geometry || m_Mesh->selected.isEmpty())
    return {};
  const QSet<uint32_t> selected = m_Mesh->selectedVertices();
  QRectF bounds;
  bool first = true;
  for(const auto &vertex : m_Mesh->geometry->vertices)
    if(selected.contains(vertex.index) && vertex.uvValid)
    {
      const QPointF p = atlasTransform().map(vertex.uv);
      if(first)
        bounds = QRectF(p, QSizeF(0, 0));
      else
        bounds = QRectF(QPointF(qMin(bounds.left(), p.x()), qMin(bounds.top(), p.y())),
                        QPointF(qMax(bounds.right(), p.x()), qMax(bounds.bottom(), p.y())));
      first = false;
    }
  bounds = bounds.adjusted(-6, -6, 6, 6).intersected(rect());
  return first ? QRectF()
               : QRectF(bounds.x() / qMax(1, width()), bounds.y() / qMax(1, height()),
                        bounds.width() / qMax(1, width()), bounds.height() / qMax(1, height()));
}

void MeshUVView::selectUVRegion(const QRectF &bounds, bool restrictToSelection)
{
  if(!m_Mesh->geometry)
    return;
  QSet<quint64> found;
  const auto &geometry = *m_Mesh->geometry;
  QPainterPath region;
  region.addRect(bounds);
  for(int face = 0; face < geometry.triangles.size(); ++face)
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
    {
      const quint64 key = m_Mesh->key(face, corner);
      if(restrictToSelection && !m_Mesh->selected.contains(key))
        continue;
      const int count = m_Mesh->mode == MeshUVData::Selection::Face   ? 3
                        : m_Mesh->mode == MeshUVData::Selection::Edge ? 2
                                                                      : 1;
      QPainterPath shape;
      bool valid = true;
      for(int j = 0; j < count; ++j)
      {
        const auto &v = geometry.vertices[geometry.triangles[face][(corner + j) % 3]];
        valid &= v.uvValid;
        if(j == 0)
          shape.moveTo(v.uv);
        else
          shape.lineTo(v.uv);
      }
      if(count == 3)
        shape.closeSubpath();
      if(valid && (region.intersects(shape) || region.contains(shape.currentPosition())))
        found.insert(key);
    }
  m_Mesh->selected = found;
  notifySelection();
}

void MeshUVView::updateWireCache(double density)
{
  // Rasterize the static overlay into discrete zoom levels. Small pans and resizes just blit it.
  const QRectF bounds = m_Mesh->geometry->bounds;
  const double span = qMax(bounds.width(), bounds.height());
  const double level = qMin(2044.0, qMax(128.0, std::pow(2.0, std::ceil(std::log2(density * span)))));
  if(level == m_WireDensity)
    return;
  m_WireDensity = level;
  const double scale = level / span;
  m_WireBounds = bounds.adjusted(-2 / scale, -2 / scale, 2 / scale, 2 / scale);
  m_Wire = QPixmap(QSize(qCeil(m_WireBounds.width() * scale), qCeil(m_WireBounds.height() * scale)));
  m_Wire.fill(Qt::transparent);
  QPainter cache(&m_Wire);
  cache.setRenderHint(QPainter::Antialiasing);
  cache.scale(m_Wire.width() / m_WireBounds.width(), m_Wire.height() / m_WireBounds.height());
  cache.translate(-m_WireBounds.topLeft());
  cache.setPen(QPen(QColor(125, 187, 208, 170), 0));
  cache.drawPath(m_Mesh->geometry->wire);
}

void MeshUVView::paintEvent(QPaintEvent *)
{
  QPainter painter(this);
  painter.fillRect(rect(), palette().brush(QPalette::Dark));
  if(!m_Mesh->geometry || m_Mesh->geometry->wire.isEmpty())
  {
    painter.setPen(palette().color(QPalette::Text));
    painter.drawText(rect(), Qt::AlignCenter | Qt::TextWordWrap,
                     tr("No UV triangles for this stage and channel."));
    return;
  }

  const QTransform transform = atlasTransform();
  const QRectF tile = transform.mapRect(QRectF(0, 0, 1, 1));
  painter.fillRect(tile, palette().brush(QPalette::Base));
  if(!m_Texture.isNull())
  {
    if(m_Tiled)
    {
      QBrush repeated(m_Texture);
      QTransform mapping;
      mapping.translate(tile.left(), tile.top());
      mapping.scale(tile.width() / m_Texture.width(), tile.height() / m_Texture.height());
      repeated.setTransform(mapping);
      painter.fillRect(rect(), repeated);
    }
    else
      painter.drawPixmap(tile, m_Texture, QRectF(m_Texture.rect()));
  }
  painter.setPen(palette().color(QPalette::Mid));
  for(int i = 1; i < 10; ++i)
  {
    const double t = i / 10.0;
    painter.drawLine(transform.map(QPointF(t, 0)), transform.map(QPointF(t, 1)));
    painter.drawLine(transform.map(QPointF(0, t)), transform.map(QPointF(1, t)));
  }
  painter.setPen(palette().color(QPalette::Text));
  painter.drawRect(tile);
  painter.drawText(tile.bottomLeft() + QPointF(0, 12), QLatin1String("0, 0"));
  painter.drawText(tile.topRight() + QPointF(-24, -2), QLatin1String("1, 1"));

  updateWireCache(transform.m11() * devicePixelRatioF() * m_SnapshotScale);
  painter.save();
  painter.setTransform(transform);
  painter.drawPixmap(m_WireBounds, m_Wire, QRectF(m_Wire.rect()));
  painter.setPen(QPen(SelectionColor, 0));
  painter.setBrush(QColor(255, 155, 52, 85));
  painter.drawPath(m_SelectedFaces);
  QPen pen(SelectionColor, 2);
  pen.setCosmetic(true);
  painter.setPen(pen);
  painter.drawLines(m_SelectedEdges);
  painter.restore();

  if(m_Mesh->mode == MeshUVData::Selection::Point)
  {
    painter.setRenderHint(QPainter::Antialiasing);
    painter.setPen(Qt::NoPen);
    painter.setBrush(QColor(125, 187, 208));
    const QRectF visible = QRectF(rect()).adjusted(-4, -4, 4, 4);
    for(const auto &vertex : m_Mesh->geometry->vertices)
    {
      const QPointF center = transform.map(vertex.uv);
      if(vertex.uvValid && visible.contains(center))
        painter.drawEllipse(center, 1.8, 1.8);
    }
    painter.setBrush(SelectionColor);
    for(const QPointF &uv : m_SelectedPoints)
    {
      const QPointF center = transform.map(uv);
      if(visible.contains(center))
        painter.drawEllipse(center, 3.5, 3.5);
    }
  }

  if(m_DraggingBox)
  {
    painter.setPen(QPen(SelectionColor, 1, Qt::DashLine));
    painter.setBrush(Qt::NoBrush);
    painter.drawRect(QRectF(m_Press, m_Last).normalized());
  }
}

void MeshUVView::selectAt(const QPointF &point, bool extend)
{
  if(!m_Mesh->geometry)
    return;
  const auto &geometry = *m_Mesh->geometry;
  const QTransform transform = atlasTransform();
  int found = -1, element = 0;
  double distance = 8.0;
  for(int index = 0; index < geometry.triangles.size(); ++index)
  {
    const auto &face = geometry.triangles[index];
    if(!geometry.vertices[face[0]].uvValid || !geometry.vertices[face[1]].uvValid ||
       !geometry.vertices[face[2]].uvValid)
      continue;
    const QPointF a = transform.map(geometry.vertices[face[0]].uv);
    const QPointF b = transform.map(geometry.vertices[face[1]].uv);
    const QPointF c = transform.map(geometry.vertices[face[2]].uv);
    const QPointF points[] = {a, b, c};
    if(m_Mesh->mode == MeshUVData::Selection::Face)
    {
      double u, v, w;
      if(barycentric(point, a, b, c, u, v, w))
        found = index;
      continue;
    }
    for(int corner = 0; corner < 3; ++corner)
    {
      const double score = m_Mesh->mode == MeshUVData::Selection::Point
                               ? QLineF(point, points[corner]).length()
                               : segmentDistance(point, points[corner], points[(corner + 1) % 3]);
      if(score < distance)
      {
        distance = score;
        found = index;
        element = corner;
      }
    }
  }
  selectElement(found, element, extend);
}

void MeshUVView::selectMesh(const QPointF &point, const QSize &size, const QMatrix4x4 &transform,
                            bool homogenous, bool extend)
{
  if(!m_Mesh->geometry)
    return;
  const auto &geometry = *m_Mesh->geometry;
  const ProjectedMesh mesh(geometry, size, transform, homogenous, meshPosition);
  double depth = std::numeric_limits<double>::infinity();
  int triangle = -1;
  for(int i = 0; i < geometry.triangles.size(); ++i)
  {
    const auto polygon = mesh.face(geometry.triangles[i]);
    if(polygon.count < 3)
      continue;
    const QVector4D &a = polygon.vertices[0];
    const QPointF screen = mesh.point(a);
    for(int j = 1; j + 1 < polygon.count; ++j)
    {
      const QVector4D &b = polygon.vertices[j], &c = polygon.vertices[j + 1];
      double u, v, w;
      if(barycentric(point, screen, mesh.point(b), mesh.point(c), u, v, w))
      {
        const double hit = u * a.z() / a.w() + v * b.z() / b.w() + w * c.z() / c.w();
        if(hit < depth)
        {
          depth = hit;
          triangle = i;
        }
      }
    }
  }

  int corner = 0;
  if(m_Mesh->mode != MeshUVData::Selection::Face)
  {
    // Prefer the components of the frontmost face. At a silhouette, also accept hits
    // within a few pixels of a visible point or the visible part of a clipped edge.
    const int begin = triangle >= 0 ? triangle : 0;
    const int end = triangle >= 0 ? triangle + 1 : geometry.triangles.size();
    double closest = triangle >= 0 ? std::numeric_limits<double>::infinity() : 8.0;
    triangle = -1;
    for(int i = begin; i < end; ++i)
    {
      for(int j = 0; j < 3; ++j)
      {
        QPointF a, b;
        if(!mesh.component(geometry.triangles[i], j, m_Mesh->mode, a, b))
          continue;
        const double distance = m_Mesh->mode == MeshUVData::Selection::Point
                                    ? QLineF(point, a).length()
                                    : segmentDistance(point, a, b);
        if(distance < closest)
        {
          closest = distance;
          triangle = i;
          corner = j;
        }
      }
    }
  }
  selectElement(triangle, corner, extend);
}

void MeshUVView::selectElement(int triangle, int corner, bool extend)
{
  if(!extend)
    m_Mesh->selected.clear();
  if(triangle >= 0)
  {
    const quint64 key = m_Mesh->key(triangle, corner);
    if(extend && m_Mesh->selected.contains(key))
      m_Mesh->selected.remove(key);
    else
      m_Mesh->selected.insert(key);
  }
  notifySelection();
}

void MeshUVView::notifySelection()
{
  m_SelectedFaces = QPainterPath();
  m_SelectedFaces.setFillRule(Qt::WindingFill);
  m_SelectedEdges.clear();
  m_SelectedPoints.clear();
  if(m_Mesh->geometry && !m_Mesh->selected.isEmpty())
  {
    const auto &geometry = *m_Mesh->geometry;
    if(m_Mesh->mode == MeshUVData::Selection::Point)
    {
      for(const auto &v : geometry.vertices)
        if(v.uvValid && m_Mesh->selected.contains(v.index))
          m_SelectedPoints.push_back(v.uv);
    }
    for(int i = 0; m_Mesh->mode != MeshUVData::Selection::Point && i < geometry.triangles.size(); ++i)
    {
      const auto &face = geometry.triangles[i];
      for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      {
        if(!m_Mesh->selected.contains(m_Mesh->key(i, corner)))
          continue;
        const QPointF a = geometry.vertices[face[corner]].uv;
        if(m_Mesh->mode == MeshUVData::Selection::Edge)
          m_SelectedEdges.push_back(QLineF(a, geometry.vertices[face[(corner + 1) % 3]].uv));
        else
        {
          m_SelectedFaces.moveTo(a);
          const QPointF b = geometry.vertices[face[1]].uv, c = geometry.vertices[face[2]].uv;
          const bool ccw = cross(b - a, c - a) >= 0.0;
          m_SelectedFaces.lineTo(ccw ? b : c);
          m_SelectedFaces.lineTo(ccw ? c : b);
          m_SelectedFaces.closeSubpath();
        }
      }
    }
  }
  setProperty("uvSelectionCount", m_Mesh->selected.size());
  update();
  if(selectionChanged)
    selectionChanged();
}

void MeshUVView::selectAll()
{
  if(m_Mesh->geometry)
    for(int face = 0; face < m_Mesh->geometry->triangles.size(); ++face)
      for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
        m_Mesh->selected.insert(m_Mesh->key(face, corner));
  notifySelection();
}

void MeshUVView::selectLinked()
{
  if(!m_Mesh->geometry || m_Mesh->selected.isEmpty())
    return;
  const auto &geometry = *m_Mesh->geometry;
  QMultiHash<uint32_t, int> adjacent;
  QVector<int> pending;
  QSet<int> visited;
  for(int face = 0; face < geometry.triangles.size(); ++face)
  {
    for(int row : geometry.triangles[face])
      adjacent.insert(geometry.vertices[row].index, face);
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      if(m_Mesh->selected.contains(m_Mesh->key(face, corner)) && !visited.contains(face))
      {
        visited.insert(face);
        pending.push_back(face);
      }
  }
  QSet<uint32_t> visitedVertices;
  for(int i = 0; i < pending.size(); ++i)
  {
    const int face = pending[i];
    for(int row : geometry.triangles[face])
    {
      const uint32_t vertex = geometry.vertices[row].index;
      if(visitedVertices.contains(vertex))
        continue;
      visitedVertices.insert(vertex);
      const auto end = adjacent.constEnd();
      for(auto other = adjacent.constFind(vertex); other != end && other.key() == vertex; ++other)
        if(!visited.contains(other.value()))
        {
          visited.insert(other.value());
          pending.push_back(other.value());
        }
    }
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      m_Mesh->selected.insert(m_Mesh->key(face, corner));
  }
  notifySelection();
}

void MeshUVView::selectLinkedFrom(const std::function<void()> &pick)
{
  if(!m_Mesh->geometry)
    return;
  const auto previous = m_Mesh->selected;
  const auto mode = m_Mesh->mode;
  const auto callback = selectionChanged;
  selectionChanged = {};
  m_Mesh->mode = MeshUVData::Selection::Face;
  m_Mesh->selected.clear();
  pick();
  selectLinked();
  m_Mesh->changeMode(mode);
  m_Mesh->selected.unite(previous);
  selectionChanged = callback;
  notifySelection();
}

void MeshUVView::selectLinkedAt(const QPointF &point)
{
  selectLinkedFrom([this, point]() { selectAt(point, false); });
}

void MeshUVView::selectMeshLinked(const QPointF &point, const QSize &size,
                                  const QMatrix4x4 &transform, bool homogenous)
{
  selectLinkedFrom([=]() { selectMesh(point, size, transform, homogenous, false); });
}

void MeshUVView::clearSelection()
{
  m_Mesh->selected.clear();
  notifySelection();
}

void MeshUVView::setBoxSelect(bool enabled)
{
  m_BoxSelect = enabled;
  setCursor(enabled ? Qt::CrossCursor : Qt::ArrowCursor);
}

void MeshUVView::mousePressEvent(QMouseEvent *event)
{
  setFocus();
  m_Press = m_Last = event->localPos();
  m_Panning = event->button() == Qt::MiddleButton;
  m_DraggingBox = event->button() == Qt::LeftButton && m_BoxSelect;
}

void MeshUVView::mouseMoveEvent(QMouseEvent *event)
{
  const QPointF delta = event->localPos() - m_Last;
  m_Last = event->localPos();
  if(m_Panning)
    m_Pan += delta / atlasTransform().m11();
  if(m_Panning || m_DraggingBox)
    update();
}

void MeshUVView::selectBox(const QRectF &box, bool extend, const QVector<QPointF> &points)
{
  if(!extend)
    m_Mesh->selected.clear();
  const auto &geometry = *m_Mesh->geometry;
  for(int i = 0; i < geometry.triangles.size(); ++i)
  {
    const auto &face = geometry.triangles[i];
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
    {
      QPointF p = points[face[corner]];
      if(m_Mesh->mode == MeshUVData::Selection::Edge)
        p = (p + points[face[(corner + 1) % 3]]) / 2;
      else if(m_Mesh->mode == MeshUVData::Selection::Face)
        p = (p + points[face[1]] + points[face[2]]) / 3;
      if(std::isfinite(p.x()) && std::isfinite(p.y()) && box.contains(p))
        m_Mesh->selected.insert(m_Mesh->key(i, corner));
    }
  }
  notifySelection();
}

void MeshUVView::selectMeshBox(const QRectF &rect, const QSize &size, const QMatrix4x4 &transform,
                               bool homogenous, bool extend)
{
  if(!m_Mesh->geometry)
    return;
  if(!extend)
    m_Mesh->selected.clear();
  const auto &geometry = *m_Mesh->geometry;
  const ProjectedMesh mesh(geometry, size, transform, homogenous, meshPosition);
  for(int i = 0; i < geometry.triangles.size(); ++i)
  {
    const auto &face = geometry.triangles[i];
    if(m_Mesh->mode == MeshUVData::Selection::Face)
    {
      const auto polygon = mesh.face(face);
      if(polygon.count < 3)
        continue;
      QPointF center;
      for(int j = 0; j < polygon.count; ++j)
        center += mesh.point(polygon.vertices[j]);
      if(rect.contains(center / polygon.count))
        m_Mesh->selected.insert(m_Mesh->key(i, 0));
      continue;
    }
    for(int j = 0; j < 3; ++j)
    {
      QPointF a, b;
      if(!mesh.component(face, j, m_Mesh->mode, a, b))
        continue;
      if(rect.contains(m_Mesh->mode == MeshUVData::Selection::Point ? a : (a + b) / 2))
        m_Mesh->selected.insert(m_Mesh->key(i, j));
    }
  }
  notifySelection();
}

void MeshUVView::mouseReleaseEvent(QMouseEvent *event)
{
  if(event->button() == Qt::MiddleButton)
    m_Panning = false;
  if(event->button() != Qt::LeftButton)
    return;
  const bool extend = (event->modifiers() & Qt::ShiftModifier) != 0;
  const QRectF box = QRectF(m_Press, event->localPos()).normalized();
  const bool useBox = m_DraggingBox && box.width() + box.height() > 4;
  m_DraggingBox = false;
  if(useBox && m_Mesh->geometry)
  {
    const QTransform transform = atlasTransform();
    QVector<QPointF> points;
    points.reserve(m_Mesh->geometry->vertices.size());
    for(const auto &vertex : m_Mesh->geometry->vertices)
      points.push_back(transform.map(vertex.uv));
    selectBox(box, extend, points);
  }
  else
    selectAt(event->localPos(), extend);
  update();
}

void MeshUVView::wheelEvent(QWheelEvent *event)
{
  const QPoint pixels = event->pixelDelta(), angle = event->angleDelta();
  const double steps = !pixels.isNull() ? (pixels.y() ? pixels.y() : pixels.x()) / 40.0
                                        : (angle.y() ? angle.y() : angle.x()) / 120.0;
  if(steps != 0.0)
  {
    const double before = atlasTransform().m11();
    m_Zoom = qBound(.01, m_Zoom * std::pow(1.2, steps), 50.0);
    const double after = atlasTransform().m11();
#if QT_VERSION >= QT_VERSION_CHECK(5, 14, 0)
    const QPointF offset = event->position() - QRectF(rect()).center();
#else
    const QPointF offset = event->posF() - QRectF(rect()).center();
#endif
    m_Pan += offset * (1.0 / after - 1.0 / before);
    setProperty("uvZoom", m_Zoom);
    update();
  }
  event->accept();
}

bool MeshUVView::handleSelectionShortcut(QKeyEvent *event)
{
  const Qt::KeyboardModifiers mods = event->modifiers() & ~Qt::KeypadModifier;
  const int key = event->key();
  if(mods == Qt::NoModifier && key >= Qt::Key_1 && key <= Qt::Key_3 && modeShortcut)
    modeShortcut(key - Qt::Key_1);
  else if(mods == Qt::NoModifier && key == Qt::Key_A)
    selectAll();
  else if(mods == Qt::AltModifier && key == Qt::Key_A)
    clearSelection();
  else if(mods == Qt::NoModifier && key == Qt::Key_L)
    selectLinkedAt(mapFromGlobal(QCursor::pos()));
  else if(mods == Qt::NoModifier && key == Qt::Key_B && boxShortcut)
    boxShortcut();
  else
    return false;
  event->accept();
  return true;
}

void MeshUVView::keyPressEvent(QKeyEvent *event)
{
  if(event->key() == Qt::Key_Space && event->modifiers() == Qt::NoModifier)
  {
    if(!event->isAutoRepeat())
    {
      m_Tiled = !m_Tiled;
      setProperty("uvTextureTiled", m_Tiled);
      update();
    }
    event->accept();
    return;
  }
  if(!handleSelectionShortcut(event))
    QWidget::keyPressEvent(event);
}

MeshUVTextureView::MeshUVTextureView(std::shared_ptr<MeshUVData> mesh) : m_Mesh(mesh)
{
  QSurfaceFormat fmt = format();
  fmt.setDepthBufferSize(24);
  setFormat(fmt);
}

MeshUVTextureView::~MeshUVTextureView()
{
  if(context())
  {
    disconnect(context(), nullptr, this, nullptr);
    releaseGL();
  }
}

void MeshUVTextureView::releaseGL()
{
  makeCurrent();
  m_Background.destroy();
  m_Triangles.destroy();
  m_Lines.destroy();
  m_Points.destroy();
  m_Selected.destroy();
  m_Texture.reset();
  m_Shader.reset();
  m_TextureKey = 0;
  m_UploadedGeometry.reset();
  m_SelectionDirty = true;
  doneCurrent();
}

void MeshUVTextureView::initializeGL()
{
  initializeOpenGLFunctions();
  connect(context(), &QOpenGLContext::aboutToBeDestroyed, this, &MeshUVTextureView::releaseGL,
          Qt::DirectConnection);
  m_Shader.reset(new QOpenGLShaderProgram);
  const QByteArray version = context()->isOpenGLES() ? QByteArray() : QByteArray("#version 120\n");
  const QByteArray vertex =
      version +
      "attribute highp vec4 position; attribute highp vec2 uv, pointCorner;"
      "uniform highp mat4 transform; uniform bool homogenous;"
      "uniform highp float depthBias, pointSize;"
      "uniform highp vec2 viewportSize; uniform bool checkerboard, roundPoint;"
      "varying highp vec2 texcoord, circle;"
      "void main() {"
      "  if (checkerboard) { gl_Position = position; texcoord = circle = vec2(0.0); return; }"
      "  gl_Position = transform * vec4(position.xyz, homogenous ? position.w : 1.0);"
      "  gl_Position.z = gl_Position.z * 2.0 - gl_Position.w;"
      "  gl_Position.z -= gl_Position.w * depthBias;"
      "  if (roundPoint) gl_Position.xy += pointCorner * pointSize / viewportSize * gl_Position.w;"
      "  circle = pointCorner; texcoord = uv;"
      "}";
  const QByteArray fragment =
      version +
      "uniform sampler2D image; uniform lowp vec4 color; uniform bool textured, flipY, roundPoint;"
      "uniform bool checkerboard; uniform lowp vec3 checkerDark, checkerLight;"
      "uniform highp float checkerHeight;"
      "varying highp vec2 texcoord, circle;"
      "void main() {"
      "  if (checkerboard) {"
      "    highp vec2 cell = floor(vec2(gl_FragCoord.x, checkerHeight - gl_FragCoord.y) / 64.0);"
      "    gl_FragColor = vec4(mod(cell.x + cell.y, 2.0) == 0.0 ? checkerLight : checkerDark, 1.0);"
      "    return;"
      "  }"
      "  if (roundPoint && dot(circle, circle) > 1.0) discard;"
      "  highp vec2 coord = fract(texcoord);"
      "  if (flipY) coord.y = 1.0 - coord.y;"
      "  gl_FragColor = textured ? texture2D(image, coord) : color;"
      "}";
  if(!m_Shader->addShaderFromSourceCode(QOpenGLShader::Vertex, vertex) ||
     !m_Shader->addShaderFromSourceCode(QOpenGLShader::Fragment, fragment) || !m_Shader->link() ||
     !m_Background.create() || !m_Triangles.create() || !m_Lines.create() || !m_Points.create() ||
     !m_Selected.create())
  {
    m_Shader.reset();
    if(unavailable)
      unavailable();
    return;
  }
  const QVector4D quad[] = {{-1, -1, 0, 1}, {1, -1, 0, 1}, {-1, 1, 0, 1}, {1, 1, 0, 1}};
  m_Background.bind();
  m_Background.allocate(quad, sizeof(quad));
  m_Background.release();
}

void MeshUVTextureView::setTransform(const QMatrix4x4 &transform, bool homogenous, bool wireframe,
                                     float nearPlane)
{
  m_Transform = transform;
  m_Homogenous = homogenous;
  m_Wireframe = wireframe;
  m_HighlightDepthBias = qMin(0.001f, nearPlane * 0.01f);
  update();
}

void MeshUVTextureView::setSelection(QVector<QVector4D> vertices, unsigned int primitive)
{
  m_Selection = std::move(vertices);
  m_SelectionPrimitive = primitive;
  m_SelectionDirty = true;
  update();
}

void MeshUVTextureView::paintGL()
{
  const QSize pixels = m_SnapshotSize.isEmpty() ? size() * devicePixelRatio() : m_SnapshotSize;
  const float scale = float(pixels.width()) / qMax(1.0, width() * devicePixelRatio());
  glClear(GL_DEPTH_BUFFER_BIT);
  if(!m_Shader)
    return;

  const QColor dark = Formatter::DarkCheckerColor(), light = Formatter::LightCheckerColor();
  glDisable(GL_DEPTH_TEST);
  m_Shader->bind();
  const int position = m_Shader->attributeLocation("position");
  m_Background.bind();
  m_Shader->enableAttributeArray(position);
  m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(QVector4D));
  m_Shader->setUniformValue("checkerboard", true);
  m_Shader->setUniformValue("checkerHeight", float(pixels.height()));
  m_Shader->setUniformValue("checkerDark", QVector3D(dark.redF(), dark.greenF(), dark.blueF()));
  m_Shader->setUniformValue("checkerLight", QVector3D(light.redF(), light.greenF(), light.blueF()));
  glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
  m_Shader->setUniformValue("checkerboard", false);
  if(!m_Mesh->geometry || m_Mesh->geometry->triangles.isEmpty())
  {
    m_Shader->disableAttributeArray(position);
    m_Background.release();
    m_Shader->release();
    return;
  }

  struct Vertex
  {
    QVector4D position;
    QVector2D uv;
  };
  if(m_UploadedGeometry != m_Mesh->geometry)
  {
    m_UploadedGeometry = m_Mesh->geometry;
    QVector<Vertex> triangles;
    QVector<QVector4D> lines;
    triangles.reserve(m_UploadedGeometry->triangles.size() * 3);
    lines.reserve(m_UploadedGeometry->triangles.size() * 6);
    for(const auto &face : m_UploadedGeometry->triangles)
    {
      for(int corner = 0; corner < 3; ++corner)
      {
        const auto &v = m_UploadedGeometry->vertices[face[corner]];
        triangles.push_back({v.position, QVector2D(v.uv)});
        lines.push_back(v.position);
        lines.push_back(m_UploadedGeometry->vertices[face[(corner + 1) % 3]].position);
      }
    }
    m_TriangleVertices = triangles.size();
    m_Triangles.bind();
    m_Triangles.allocate(triangles.constData(), triangles.size() * sizeof(Vertex));
    m_Lines.bind();
    m_Lines.allocate(lines.constData(), lines.size() * sizeof(QVector4D));
    QVector<MeshPointSprite> points;
    points.reserve(m_UploadedGeometry->vertices.size() * 6);
    for(const auto &vertex : m_UploadedGeometry->vertices)
      appendPointSprite(points, vertex.position);
    m_Points.bind();
    m_Points.allocate(points.constData(), points.size() * sizeof(MeshPointSprite));
  }
  const QImage displayedTexture = m_Mesh->displayTexture();
  if(m_TextureKey != displayedTexture.cacheKey())
  {
    m_TextureKey = displayedTexture.cacheKey();
    std::unique_ptr<QOpenGLTexture> replacement;
    if(!displayedTexture.isNull())
    {
      // Upload the replacement before releasing the previous texture.
      replacement.reset(new QOpenGLTexture(displayedTexture, QOpenGLTexture::DontGenerateMipMaps));
      if(replacement->isCreated())
      {
        replacement->setMinMagFilters(QOpenGLTexture::Linear, QOpenGLTexture::Linear);
        replacement->setWrapMode(QOpenGLTexture::ClampToEdge);
      }
      else
        replacement.reset();
    }
    m_Texture = std::move(replacement);
  }
  if(m_SelectionDirty)
  {
    m_SelectionDirty = false;
    m_Selected.bind();
    if(m_SelectionPrimitive == GL_POINTS)
    {
      QVector<MeshPointSprite> points;
      points.reserve(m_Selection.size() * 6);
      for(const QVector4D &point : m_Selection)
        appendPointSprite(points, point);
      m_Selected.allocate(points.constData(), points.size() * sizeof(MeshPointSprite));
    }
    else
      m_Selected.allocate(m_Selection.constData(), m_Selection.size() * sizeof(QVector4D));
  }

  glEnable(GL_DEPTH_TEST);
  glDepthFunc(GL_LEQUAL);
  m_Shader->setUniformValue("transform", m_Transform);
  m_Shader->setUniformValue("homogenous", m_Homogenous);
  m_Shader->setUniformValue("depthBias", 0.0f);
  m_Shader->setUniformValue("roundPoint", false);
  m_Shader->setUniformValue("viewportSize", QVector2D(pixels.width(), pixels.height()));
  m_Shader->setUniformValue("flipY", m_Mesh->flipTexture);
  m_Shader->setUniformValue("image", 0);
  m_Shader->setUniformValue("textured", m_Texture != nullptr);
  m_Shader->setUniformValue("color", QVector4D(.65f, .65f, .65f, 1));
  if(m_Texture)
    m_Texture->bind(0);

  m_Triangles.bind();
  const int uv = m_Shader->attributeLocation("uv");
  m_Shader->enableAttributeArray(uv);
  m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(Vertex));
  m_Shader->setAttributeBuffer(uv, GL_FLOAT, offsetof(Vertex, uv), 2, sizeof(Vertex));
  glEnable(GL_POLYGON_OFFSET_FILL);
  glPolygonOffset(1.0f, 1.0f);
  glDepthMask(GL_TRUE);
  glEnable(GL_BLEND);
  glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
  glDrawArrays(GL_TRIANGLES, 0, m_TriangleVertices);
  glDisable(GL_BLEND);
  glDisable(GL_POLYGON_OFFSET_FILL);
  glDepthMask(GL_FALSE);
  m_Shader->disableAttributeArray(uv);
  m_Shader->setUniformValue("textured", false);
  m_Shader->setUniformValue("depthBias", m_HighlightDepthBias);
  if(m_Wireframe)
  {
    m_Lines.bind();
    m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(QVector4D));
    m_Shader->setUniformValue("color", QVector4D(.18f, .18f, .18f, 1));
    glLineWidth(scale);
    glDrawArrays(GL_LINES, 0, m_TriangleVertices * 2);
    glLineWidth(1.0f);
  }
  const auto drawPoints = [&](QOpenGLBuffer &buffer, int count, float size, const QVector4D &color) {
    buffer.bind();
    const int corner = m_Shader->attributeLocation("pointCorner");
    m_Shader->enableAttributeArray(corner);
    m_Shader->setAttributeBuffer(corner, GL_FLOAT, offsetof(MeshPointSprite, corner), 2,
                                 sizeof(MeshPointSprite));
    m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(MeshPointSprite));
    m_Shader->setUniformValue("roundPoint", true);
    m_Shader->setUniformValue("pointSize", size * scale);
    m_Shader->setUniformValue("color", color);
    glDrawArrays(GL_TRIANGLES, 0, count * 6);
    m_Shader->setUniformValue("roundPoint", false);
    m_Shader->disableAttributeArray(corner);
  };
  if(m_Wireframe && m_Mesh->mode == MeshUVData::Selection::Point)
    drawPoints(m_Points, m_UploadedGeometry->vertices.size(), 3.6f, QVector4D(.60f, .79f, .87f, 1));
  if(m_Wireframe && !m_Selection.isEmpty())
  {
    const QVector4D selectionColor(1.0f, .63f, .21f, 1);
    if(m_SelectionPrimitive == GL_POINTS)
      drawPoints(m_Selected, m_Selection.size(), 8.0f, selectionColor);
    else
    {
      m_Selected.bind();
      m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(QVector4D));
      m_Shader->setUniformValue("color", selectionColor);
      glEnable(GL_POLYGON_OFFSET_FILL);
      glPolygonOffset(-1.0f, -1.0f);
      glDrawArrays(m_SelectionPrimitive, 0, m_Selection.size());
      glDisable(GL_POLYGON_OFFSET_FILL);
    }
  }
  glDepthMask(GL_TRUE);
  m_Shader->disableAttributeArray(position);
  QOpenGLBuffer::release(QOpenGLBuffer::VertexBuffer);
  m_Shader->release();
  if(m_Texture)
    m_Texture->release();
  if(rendered)
    rendered(m_Texture ? m_TextureKey : 0, m_Wireframe);
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Mesh component mode conversion preserves the selected region", "[mesh][uv]")
{
  auto geometry = std::make_shared<MeshUVGeometry>();
  geometry->vertices = {{QVector4D(0, 0, 0, 1), QPointF(0, 0), 11},
                        {QVector4D(1, 0, 0, 1), QPointF(1, 0), 22},
                        {QVector4D(0, 1, 0, 1), QPointF(0, 1), 33},
                        {QVector4D(1, 1, 0, 1), QPointF(1, 1), 44}};
  geometry->triangles = {{{0, 1, 2}}, {{1, 3, 2}}};
  MeshUVData mesh;
  mesh.geometry = geometry;
  mesh.selected.insert(0);
  const QSet<quint64> faceVertices{11, 22, 33};
  const QSet<quint64> faceSelection{0};
  mesh.changeMode(MeshUVData::Selection::Point);
  CHECK(mesh.selected == faceVertices);
  mesh.changeMode(MeshUVData::Selection::Edge);
  CHECK(mesh.selected.size() == 3);
  mesh.changeMode(MeshUVData::Selection::Face);
  CHECK(mesh.selected == faceSelection);
  mesh.changeMode(MeshUVData::Selection::Edge);
  CHECK(mesh.selected.size() == 3);
  mesh.changeMode(MeshUVData::Selection::Point);
  CHECK(mesh.selected == faceVertices);
  mesh.selectVertices({22, 44});
  mesh.changeMode(MeshUVData::Selection::Edge);
  CHECK(mesh.selected.size() == 1);
  mesh.changeMode(MeshUVData::Selection::Face);
  CHECK(mesh.selected.isEmpty());
}

TEST_CASE("Mesh navigation presets map their camera gestures", "[mesh][camera]")
{
  CHECK((meshCameraGesture(0, Qt::LeftButton, Qt::NoModifier) == MeshCameraGesture::Orbit));
  CHECK((meshCameraGesture(0, Qt::MiddleButton, Qt::NoModifier) == MeshCameraGesture::Pan));
  CHECK((meshCameraGesture(1, Qt::MiddleButton, Qt::AltModifier) == MeshCameraGesture::Orbit));
  CHECK((meshCameraGesture(1, Qt::MiddleButton, Qt::NoModifier) == MeshCameraGesture::Pan));
  CHECK((meshCameraGesture(1, Qt::MiddleButton, Qt::AltModifier | Qt::ControlModifier) ==
         MeshCameraGesture::Dolly));
  CHECK((meshCameraGesture(2, Qt::LeftButton, Qt::AltModifier) == MeshCameraGesture::Orbit));
  CHECK((meshCameraGesture(2, Qt::MiddleButton, Qt::AltModifier) == MeshCameraGesture::Pan));
  CHECK((meshCameraGesture(2, Qt::RightButton, Qt::AltModifier) == MeshCameraGesture::Dolly));
  CHECK((meshCameraGesture(2, Qt::LeftButton | Qt::MiddleButton, Qt::AltModifier) ==
         MeshCameraGesture::Dolly));
}

TEST_CASE("Mesh selection clips primitives before the perspective divide", "[mesh][uv]")
{
  MeshUVGeometry geometry;
  geometry.vertices = {
      {QVector4D(-.8f, -.6f, .35f, 1.0f), QPointF(0, 0), 0},
      {QVector4D(.8f, -.6f, .35f, 1.0f), QPointF(1, 0), 1},
      {QVector4D(0, .6f, -.8f, -.25f), QPointF(.5, 1), 2},
  };
  const std::array<int, 3> triangle = {{0, 1, 2}};
  const ProjectedMesh mesh(geometry, QSize(100, 100), QMatrix4x4(), true);
  const auto clipped = mesh.face(triangle);
  REQUIRE(clipped.count == 4);
  const auto hit = [&](const QPointF &p) {
    for(int i = 1; i + 1 < clipped.count; ++i)
    {
      double u, v, w;
      if(barycentric(p, mesh.point(clipped.vertices[0]), mesh.point(clipped.vertices[i]),
                     mesh.point(clipped.vertices[i + 1]), u, v, w))
        return true;
    }
    return false;
  };
  CHECK(hit(QPointF(50, 75)));
  CHECK_FALSE(hit(QPointF(50, 50)));

  QPointF a, b;
  REQUIRE(mesh.component(triangle, 2, MeshUVData::Selection::Edge, a, b));
  CHECK(a.y() == Approx(68.95).margin(.1));
  CHECK(b.y() == Approx(80));
  CHECK_FALSE(mesh.component(triangle, 2, MeshUVData::Selection::Point, a, b));
  REQUIRE(mesh.component(triangle, 0, MeshUVData::Selection::Point, a, b));
  CHECK(a.x() == Approx(10));
  CHECK(a.y() == Approx(80));
}
#endif

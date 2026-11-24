#pragma once

#include <QPair>
#include <QRectF>
#include <QVector>
#include <algorithm>
#include <cmath>
#include <limits>

namespace AIVisualFlow
{
constexpr double Clearance = 12.0;
using Links = QVector<QPair<int, int>>;

inline QVector<QRectF> PlaceNodes(const QVector<double> &heights, const Links &links, double width)
{
  if(heights.isEmpty())
    return {};
  QVector<int> rank(heights.size()), incoming(heights.size()), ready;
  QVector<QVector<int>> parents(heights.size()), children(heights.size());
  for(const auto &link : links)
  {
    if(link.first < 0)
      continue;
    parents[link.second].push_back(link.first);
    children[link.first].push_back(link.second);
    ++incoming[link.second];
  }
  for(int i = 0; i < incoming.size(); ++i)
    if(incoming[i] == 0)
      ready.push_back(i);
  for(int i = 0; i < ready.size(); ++i)
  {
    const int node = ready[i];
    for(int child : children[node])
    {
      rank[child] = qMax(rank[child], rank[node] + 1);
      if(--incoming[child] == 0)
        ready.push_back(child);
    }
  }
  // Delay an independent input to the column just before its first consumer.
  // Leave shared inputs far enough left to feed every consumer.
  for(int i = 0; i < heights.size(); ++i)
  {
    if(ready.size() != heights.size() || (parents[i].isEmpty() && children[i].isEmpty()))
      rank[i] = i % qMin(3, heights.size());
    else if(parents[i].isEmpty())
    {
      int firstConsumer = heights.size();
      for(int child : children[i])
        firstConsumer = qMin(firstConsumer, rank[child]);
      rank[i] = firstConsumer - 1;
    }
  }
  const int count = *std::max_element(rank.begin(), rank.end()) + 1;
  QVector<QVector<int>> columns(count);
  for(int i = 0; i < rank.size(); ++i)
    columns[rank[i]].push_back(i);

  // Sweep both ways using the relative order of connected nodes. Stable ties
  // retain the author's ordering when it does not affect the layout.
  QVector<double> order(heights.size(), .5);
  auto recordOrder = [&](const QVector<int> &column) {
    for(int i = 0; i < column.size(); ++i)
      order[column[i]] = (i + .5) / column.size();
  };
  for(const auto &column : columns)
    recordOrder(column);
  for(int sweep = 0; sweep < 6; ++sweep)
  {
    const bool downstream = sweep % 2 == 0;
    const auto &neighbors = downstream ? parents : children;
    for(int step = 0; step < count; ++step)
    {
      auto &column = columns[downstream ? step : count - 1 - step];
      auto priority = [&](int node) {
        double sum = 0;
        for(int neighbor : neighbors[node])
          sum += order[neighbor];
        return neighbors[node].isEmpty() ? order[node] : sum / neighbors[node].size();
      };
      std::stable_sort(column.begin(), column.end(),
                       [&](int a, int b) { return priority(a) < priority(b); });
      recordOrder(column);
    }
  }

  constexpr double rowGap = 46.0;
  QVector<double> center(heights.size());
  for(const auto &column : columns)
  {
    double height = -rowGap;
    for(int node : column)
      height += heights[node] + rowGap;
    double top = -height * .5;
    for(int node : column)
    {
      center[node] = top + heights[node] * .5;
      top += heights[node] + rowGap;
    }
  }
  // Align actual card centers with adjacent inputs and outputs, while
  // respecting card sizes and the ordering chosen above.
  for(int sweep = 0; sweep < 8; ++sweep)
  {
    for(int step = 0; step < count; ++step)
    {
      const auto &column = columns[sweep % 2 == 0 ? step : count - 1 - step];
      QVector<double> desired;
      for(int node : column)
      {
        double sum = center[node], weight = 1;
        for(const auto *neighbors : {&parents[node], &children[node]})
          for(int neighbor : *neighbors)
          {
            const double influence = 1.0 / qMax(1, qAbs(rank[node] - rank[neighbor]));
            sum += center[neighbor] * influence;
            weight += influence;
          }
        desired.push_back(sum / weight);
      }
      QVector<double> down = desired, up = desired;
      for(int i = 1; i < column.size(); ++i)
      {
        const double gap = (heights[column[i - 1]] + heights[column[i]]) * .5 + rowGap;
        down[i] = qMax(down[i], down[i - 1] + gap);
      }
      for(int i = column.size() - 1; i > 0; --i)
      {
        const double gap = (heights[column[i - 1]] + heights[column[i]]) * .5 + rowGap;
        up[i - 1] = qMin(up[i - 1], up[i] - gap);
      }
      for(int i = 0; i < column.size(); ++i)
        center[column[i]] = (up[i] + down[i]) * .5;
    }
  }

  QVector<QRectF> boxes;
  for(int i = 0; i < heights.size(); ++i)
    boxes.push_back(QRectF(rank[i] * (width + 100), center[i] - heights[i] * .5, width, heights[i]));
  return boxes;
}

inline QRectF Margin(const QRectF &box)
{
  return box.adjusted(-Clearance, -Clearance, Clearance, Clearance);
}

inline QPointF NearestClearNodeDisplacement(const QVector<QRectF> &boxes, int node,
                                            const QVector<QVector<QPointF>> &reroutes)
{
  const QRectF moving = Margin(boxes[node]);
  QVector<QRectF> blocked;
  auto addObstacle = [&](const QRectF &obstacle) {
    blocked.push_back(
        QRectF(QPointF(obstacle.left() - moving.right(), obstacle.top() - moving.bottom()),
               QPointF(obstacle.right() - moving.left(), obstacle.bottom() - moving.top())));
  };
  for(int i = 0; i < boxes.size(); ++i)
    if(i != node)
      addObstacle(Margin(boxes[i]));
  for(const auto &edge : reroutes)
    for(const QPointF &point : edge)
      addObstacle(QRectF(point, point).adjusted(-.001, -.001, .001, .001));

  auto isClear = [&](const QPointF &delta) {
    for(const auto &region : blocked)
      if(delta.x() > region.left() && delta.x() < region.right() && delta.y() > region.top() &&
         delta.y() < region.bottom())
        return false;
    return true;
  };
  if(isClear(QPointF()))
    return {};

  // The nearest point outside a union of rectangles is either an axis
  // projection onto an exposed side or a corner where two sides meet.
  QVector<double> x{0}, y{0};
  for(const auto &region : blocked)
  {
    x << region.left() << region.right();
    y << region.top() << region.bottom();
  }
  QPointF nearest;
  double best = std::numeric_limits<double>::infinity();
  for(double dx : x)
  {
    for(double dy : y)
    {
      const QPointF delta(dx, dy);
      const double distance = QPointF::dotProduct(delta, delta);
      if(distance < best && isClear(delta))
      {
        best = distance;
        nearest = delta;
      }
    }
  }
  return nearest;
}

struct Cubic
{
  QPointF start, first, second, end;
};

struct CurveRoute
{
  QVector<Cubic> curves;
  int side = 0;
};

struct Wire
{
  QVector<CurveRoute> legs;
};

inline bool CurveHits(const Cubic &c, const QRectF &box, int depth = 0)
{
  const double left = qMin(qMin(c.start.x(), c.first.x()), qMin(c.second.x(), c.end.x()));
  const double right = qMax(qMax(c.start.x(), c.first.x()), qMax(c.second.x(), c.end.x()));
  const double top = qMin(qMin(c.start.y(), c.first.y()), qMin(c.second.y(), c.end.y()));
  const double bottom = qMax(qMax(c.start.y(), c.first.y()), qMax(c.second.y(), c.end.y()));
  if(right < box.left() || left > box.right() || bottom < box.top() || top > box.bottom())
    return false;
  if(depth == 6 || box.contains(c.start) || box.contains(c.end))
    return true;

  // Split the control hull. A conservative test at this resolution avoids
  // accepting grazing wires without flattening a path for every trial.
  const QPointF a = (c.start + c.first) * .5, b = (c.first + c.second) * .5;
  const QPointF d = (c.second + c.end) * .5;
  const QPointF ab = (a + b) * .5, bd = (b + d) * .5, mid = (ab + bd) * .5;
  return CurveHits({c.start, a, ab, mid}, box, depth + 1) ||
         CurveHits({mid, bd, d, c.end}, box, depth + 1);
}

inline QPointF UnitTangent(const QPointF &delta)
{
  const double length = std::hypot(delta.x(), delta.y());
  return length > 0 ? delta / length : QPointF(1, 0);
}

inline Cubic MakeCubic(const QPointF &start, const QPointF &end, const QPointF &outgoing,
                       const QPointF &incoming, double startReach = 160.0, double endReach = 160.0)
{
  const double reach = std::hypot(end.x() - start.x(), end.y() - start.y()) * .42;
  return {start, start + outgoing * qMin(startReach, reach), end - incoming * qMin(endReach, reach),
          end};
}

inline int BlockedCards(const CurveRoute &route, const QVector<QRectF> &obstacles)
{
  int count = 0;
  for(const QRectF &box : obstacles)
    for(const Cubic &curve : route.curves)
      if(CurveHits(curve, box))
      {
        ++count;
        break;
      }
  return count;
}

inline int BlockedCards(const Cubic &curve, const QVector<QRectF> &obstacles)
{
  return int(std::count_if(obstacles.cbegin(), obstacles.cend(),
                           [&](const QRectF &box) { return CurveHits(curve, box); }));
}

inline CurveRoute RouteCurve(const QPointF &start, const QPointF &end, const QPointF &outgoing,
                             const QPointF &incoming, const QVector<QRectF> &obstacles,
                             const CurveRoute &previous, int pointsAvailable)
{
  const Cubic base = MakeCubic(start, end, outgoing, incoming);
  CurveRoute best{{base}, previous.side};
  QVector<QRectF> blockers;
  for(const QRectF &box : obstacles)
    if(CurveHits(base, box))
      blockers.push_back(box);
  if(blockers.isEmpty())
    return best;

  int fewestHits = blockers.size();
  const int preferred = previous.side != 0 ? previous.side : -1;
  double bestBend = std::numeric_limits<double>::infinity();
  const double limit = qBound(34.0, (end - start).manhattanLength() * .32, 128.0);
  for(int side : {preferred, -preferred})
  {
    for(const QPointF weights : {QPointF(1, 1), QPointF(1, .35), QPointF(.35, 1)})
    {
      auto bent = [&](double amount) {
        return Cubic{base.start, base.first + QPointF(0, weights.x() * side * amount),
                     base.second + QPointF(0, weights.y() * side * amount), base.end};
      };
      double clear = limit;
      int hits = BlockedCards(bent(clear), obstacles);
      if(hits < fewestHits)
      {
        fewestHits = hits;
        best = {{bent(clear)}, side};
      }
      // Try a smaller bend as well: the full bend can run into a different card.
      if(hits > 0)
      {
        clear *= .5;
        hits = BlockedCards(bent(clear), obstacles);
        if(hits < fewestHits)
        {
          fewestHits = hits;
          best = {{bent(clear)}, side};
        }
      }
      if(hits > 0)
        continue;
      double blocked = 0;
      for(int i = 0; i < 7; ++i)
      {
        const double middle = (blocked + clear) * .5;
        if(BlockedCards(bent(middle), obstacles) == 0)
          clear = middle;
        else
          blocked = middle;
      }
      if(clear < bestBend)
      {
        bestBend = clear;
        best = {{bent(clear)}, side};
      }
    }
    // Keep the existing side when it still works.
    if(fewestHits == 0 && previous.side != 0)
      return best;
  }
  if(fewestHits == 0)
    return best;

  QRectF span = blockers.front();
  for(const QRectF &box : blockers)
    span |= box;

  const double direction = end.x() >= start.x() ? 1 : -1;
  const QPointF through(direction, 0);
  for(int count = 1; count <= pointsAvailable; ++count)
  {
    double shortest = std::numeric_limits<double>::infinity();
    for(int side : {preferred, -preferred})
    {
      for(const QRectF &box : {span, blockers.front()})
      {
        for(double gap : {4.0, 14.0, 36.0, 72.0})
        {
          const double y = side < 0 ? box.top() - gap : box.bottom() + gap;
          const double left = box.left() - gap, right = box.right() + gap;
          const QPointF entry(count == 1 ? box.center().x() : (direction > 0 ? left : right), y);
          const QPointF exit = count == 1 ? entry : QPointF(direction > 0 ? right : left, y);
          const double length = (entry - start).manhattanLength() +
                                (exit - entry).manhattanLength() + (end - exit).manhattanLength();
          for(double reach : {72.0, 24.0, 8.0})
          {
            CurveRoute detour{{MakeCubic(start, entry, outgoing, through, 160, reach)}, side};
            if(count == 2)
              detour.curves.push_back(MakeCubic(entry, exit, through, through, reach, reach));
            detour.curves.push_back(MakeCubic(exit, end, through, incoming, reach, 160));
            const int hits = BlockedCards(detour, obstacles);
            if(hits < fewestHits || (hits == 0 && length < shortest))
            {
              fewestHits = hits;
              shortest = length;
              best = detour;
            }
            if(hits == 0)
              break;
          }
        }
      }
      if(fewestHits == 0 && previous.side != 0)
        return best;
    }
    if(fewestHits == 0)
      return best;
  }
  return best;
}

inline CurveRoute ContinueRoute(const CurveRoute &previous, const CurveRoute &target,
                                const QVector<QRectF> &obstacles)
{
  const int count = target.curves.size();
  if(previous.side == 0 || previous.side != target.side || previous.curves.size() != count)
    return target;

  // Keep the ports attached, then ease the handles and automatic points
  // toward the new route. Never ease through an obstacle the new route clears.
  const QPointF startShift = target.curves.front().start - previous.curves.front().start;
  const QPointF endShift = target.curves.back().end - previous.curves.back().end;
  CurveRoute eased = target;
  for(int i = 0; i < count; ++i)
  {
    const Cubic &old = previous.curves[i], &next = target.curves[i];
    const QPointF before = startShift + (endShift - startShift) * (double(i) / count);
    const QPointF after = startShift + (endShift - startShift) * (double(i + 1) / count);
    eased.curves[i] = {(old.start + before + next.start) * .5, (old.first + before + next.first) * .5,
                       (old.second + after + next.second) * .5, (old.end + after + next.end) * .5};
  }
  eased.curves.front().start = target.curves.front().start;
  eased.curves.back().end = target.curves.back().end;
  return BlockedCards(eased, obstacles) == 0 ? eased : target;
}

inline QVector<Wire> RouteWires(const QVector<QRectF> &boxes, const Links &links,
                                const QVector<QRectF> &automatic,
                                const QVector<QVector<QPointF>> &reroutes,
                                const QVector<Wire> &previous)
{
  QVector<Wire> wires(links.size());
  if(boxes.isEmpty() || links.isEmpty())
    return wires;

  QVector<QVector<int>> ports(boxes.size() * 2);
  QVector<QPointF> start(links.size()), end(links.size());
  QVector<int> facing(links.size(), 1);
  for(int i = 0; i < links.size(); ++i)
  {
    const auto link = links[i];
    if(link.first < 0 || link.first == link.second)
      continue;
    facing[i] = automatic[link.second].center().x() < automatic[link.first].center().x() ? -1 : 1;
    ports[link.first * 2 + (facing[i] > 0)].push_back(i * 2);
    ports[link.second * 2 + (facing[i] < 0)].push_back(i * 2 + 1);
  }
  for(int side = 0; side < ports.size(); ++side)
  {
    auto &port = ports[side];
    auto otherY = [&](int ref) {
      const auto link = links[ref / 2];
      return automatic[ref % 2 == 0 ? link.second : link.first].center().y();
    };
    std::stable_sort(port.begin(), port.end(), [&](int a, int b) { return otherY(a) < otherY(b); });
    const QRectF box = boxes[side / 2];
    const double spacing = qMin(17.0, (box.height() - 10) / qMax(1, port.size() - 1));
    for(int j = 0; j < port.size(); ++j)
    {
      const int ref = port[j];
      (ref % 2 == 0 ? start : end)[ref / 2] =
          QPointF(side % 2 == 0 ? box.left() : box.right(),
                  box.center().y() + (j - (port.size() - 1) * .5) * spacing);
    }
  }

  for(int i = 0; i < links.size(); ++i)
  {
    const auto link = links[i];
    if(link.first < 0 || link.first == link.second)
      continue;
    QVector<QRectF> obstacles;
    for(int node = 0; node < boxes.size(); ++node)
    {
      const QRectF box = node == link.first || node == link.second
                             ? boxes[node].adjusted(1, 1, -1, -1)
                             : boxes[node].adjusted(-5, -5, 5, 5);
      if(!box.contains(start[i]) && !box.contains(end[i]))
        obstacles.push_back(box);
    }

    QVector<QPointF> stops{start[i]};
    stops += reroutes.value(i);
    stops.push_back(end[i]);
    QVector<QPointF> tangents(stops.size(), QPointF(facing[i], 0));
    for(int point = 1; point + 1 < stops.size(); ++point)
      tangents[point] = UnitTangent(stops[point + 1] - stops[point - 1]);

    int pointsAvailable = 2;
    for(int leg = 1; leg < stops.size(); ++leg)
    {
      const CurveRoute old = i < previous.size() && previous[i].legs.size() == stops.size() - 1
                                 ? previous[i].legs[leg - 1]
                                 : CurveRoute();
      CurveRoute route = RouteCurve(stops[leg - 1], stops[leg], tangents[leg - 1], tangents[leg],
                                    obstacles, old, pointsAvailable);
      route = ContinueRoute(old, route, obstacles);
      pointsAvailable -= route.curves.size() - 1;
      wires[i].legs.push_back(route);
    }
  }
  return wires;
}
}    // namespace AIVisualFlow

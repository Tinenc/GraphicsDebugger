#pragma once

#include <QCryptographicHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include "Code/Interface/QRDInterface.h"
#include "Code/QRDUtils.h"

// Native resource snapshots are reference data, never instructions or replay authority. All JSON
// keys/tokens below are wire identifiers. Keep 64-bit IDs/offsets as strings across Qt and Python.
inline QJsonObject ReplayLabAIResourceContext(ICaptureContext &ctx, ResourceId id,
                                             const char *panel)
{
  const QString name = ctx.GetResourceName(id);
  const QString identity = QString(ctx.GetCaptureFilename()) + QLatin1Char('\n') +
                           QString::number(ctx.CurEvent()) + QLatin1Char('\n') + ToQStr(id);
  QJsonObject origin;
  origin[QLatin1String("kind")] = QLatin1String("resource-selection");
  origin[QLatin1String("panel")] = QLatin1String(panel);
  origin[QLatin1String("sourceId")] = QLatin1String("resource-") + QString::fromLatin1(
      QCryptographicHash::hash(identity.toUtf8(), QCryptographicHash::Sha256).toHex());
  // Resource names may be asset paths; keep them in resource.name rather than path-free origin.
  QJsonObject capture;
  capture[QLatin1String("eventId")] = int(ctx.CurEvent());
  capture[QLatin1String("selectedEventId")] = int(ctx.CurSelectedEvent());
  QJsonObject resource;
  resource[QLatin1String("resourceId")] = ToQStr(id);
  resource[QLatin1String("name")] = name;
  resource[QLatin1String("kind")] = ctx.GetTexture(id) ? QLatin1String("texture")
                                                      : QLatin1String("buffer");
  QJsonObject context;
  context[QLatin1String("schema")] = QLatin1String("replaylab-attachment-context/v1");
  context[QLatin1String("origin")] = origin;
  context[QLatin1String("capture")] = capture;
  context[QLatin1String("resource")] = resource;
  return context;
}

inline QJsonObject ReplayLabAIResourceDetails(ICaptureContext &ctx, ResourceId id,
                                             const QJsonObject &context)
{
  QJsonObject details = context;
  QJsonArray bindings;
  int total = 0;
  const PipeState &pipe = ctx.CurPipelineState();
  const ShaderStage stages[] = {ShaderStage::Vertex, ShaderStage::Hull, ShaderStage::Domain,
                                ShaderStage::Geometry, ShaderStage::Pixel, ShaderStage::Compute,
                                ShaderStage::Task, ShaderStage::Mesh};
  for(ShaderStage stage : stages)
  {
    if(pipe.GetShader(stage) == ResourceId())
      continue;
    const ShaderReflection *reflection = pipe.GetShaderReflection(stage);
    auto add = [&](const rdcarray<UsedDescriptor> &descriptors, const char *kind) {
      for(const UsedDescriptor &desc : descriptors)
      {
        if(desc.descriptor.resource != id)
          continue;
        ++total;
        if(bindings.size() >= 64)
          continue;
        QJsonObject binding;
        binding[QLatin1String("stage")] = ToQStr(stage);
        binding[QLatin1String("shaderResourceId")] = ToQStr(pipe.GetShader(stage));
        binding[QLatin1String("entryPoint")] = QString(pipe.GetShaderEntryPoint(stage));
        binding[QLatin1String("kind")] = QLatin1String(kind);
        binding[QLatin1String("index")] = int(desc.access.index);
        binding[QLatin1String("arrayIndex")] = int(desc.access.arrayElement);
        binding[QLatin1String("byteOffset")] = QString::number(desc.descriptor.byteOffset);
        binding[QLatin1String("byteSize")] = QString::number(desc.descriptor.byteSize);
        if(reflection && QLatin1String(kind) == QLatin1String("cbuffer") &&
           desc.access.index < reflection->constantBlocks.size())
        {
          const ConstantBlock &block = reflection->constantBlocks[desc.access.index];
          binding[QLatin1String("name")] = QString(block.name);
          binding[QLatin1String("binding")] = int(block.fixedBindNumber);
          binding[QLatin1String("registerSpace")] = int(block.fixedBindSetOrSpace);
          binding[QLatin1String("reflectedByteSize")] = QString::number(block.byteSize);
        }
        bindings.append(binding);
      }
    };
    add(pipe.GetConstantBlocks(stage, true), "cbuffer");
    add(pipe.GetReadOnlyResources(stage, true), "read-only");
    add(pipe.GetReadWriteResources(stage, true), "read-write");
  }
  details[QLatin1String("bindingsAtEvent")] = bindings;
  details[QLatin1String("bindingCount")] = total;
  details[QLatin1String("bindingsTruncated")] = total > bindings.size();
  if(const TextureDescription *texture = ctx.GetTexture(id))
  {
    QJsonObject description;
    description[QLatin1String("width")] = int(texture->width);
    description[QLatin1String("height")] = int(texture->height);
    description[QLatin1String("depth")] = int(texture->depth);
    description[QLatin1String("format")] = QString(texture->format.Name());
    details[QLatin1String("texture")] = description;
  }
  return details;
}

inline QString ReplayLabAIResourceJSON(const QJsonObject &object)
{
  return QString::fromUtf8(QJsonDocument(object).toJson(QJsonDocument::Compact));
}

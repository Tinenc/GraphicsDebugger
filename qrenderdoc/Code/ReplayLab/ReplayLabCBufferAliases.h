/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 The RenderDoc Authors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#pragma once

#include <QCoreApplication>
#include <QHash>
#include <QList>
#include <QString>
#include "Code/Interface/QRDInterface.h"

struct ConstantBlock;
struct CBufferDebugDisplaySource;
class QWidget;
class QJsonObject;

// One GUI capture's explicit preview provenance. This is never an RID-only fallback and never
// writes the alias store. Independent of Qt widgets so its owner/layout rules can be tested.
class ReplayLabCBufferDebugDisplayRegistry
{
public:
  bool Register(const QString &owner, ResourceId original, ResourceId replacement,
                const QString &sourceSha, const QString &activeSha, ShaderStage stage,
                uint64_t mutation, const rdcarray<ConstantBlock> &sourceBlocks,
                const rdcarray<ConstantBlock> &activeBlocks);
  void Remove(const QString &owner);
  void Clear() { m_Entries.clear(); }
  CBufferDebugDisplaySource Resolve(ResourceId original, ShaderStage stage,
                                   const QString &activeSha, const ConstantBlock &activeBlock,
                                   uint64_t mutation) const;

private:
  struct BlockPair
  {
    ConstantBlock source;
    ConstantBlock active;
  };
  struct Entry
  {
    QString owner, sourceSha, activeSha;
    ResourceId original, replacement;
    ShaderStage stage = ShaderStage::Count;
    uint64_t mutation = 0;
    QList<BlockPair> blocks;
  };
  QList<Entry> m_Entries;
};

// Identity of one reflected constant-buffer variable inside its block, as the CBuffer panel
// shows it.
struct ReplayLabCBufferAliasKey
{
  QString path;       // reflected variable path: "v0", "light.color", "bones[2]"
  qint64 offset = -1; // absolute byte offset of a leaf inside the block, -1 for non-leaves
};

struct ReplayLabCBufferAlias
{
  QString alias;
  QString source;    // "user" or "ai"
  bool valid() const { return !alias.isEmpty(); }
};

// A display-only projection of physical 32-bit lanes. Offsets stay relative to the captured
// block; this never becomes reflection or a compiler packing declaration. Live editing must
// separately resolve and validate every projected lane against the current physical reflection.
struct ReplayLabCBufferSemanticField
{
  QString name;
  QString type;
  QList<qint64> offsets;
  QString status;
  QString provenance;
  QString evidence;
  QString description;
  QString sourceSha256;
  QString source; // Preserve explicit user labels when grouping AI array presentation.
};

// Per-user display names for constant-buffer blocks and variables. The captured shader and every
// workspace source keep their original identifiers; an alias only changes what the panel shows.
//
// Aliases normally follow the constant block's *layout* (name + top-level members and offsets).
// A stripped unknown placeholder layout has no material semantic identity, so its AI aliases are
// additionally scoped by original raw Shader SHA-256, optional captured Shader resource ID,
// stage, bind, space, and layout SHA-256. Resource IDs are diagnostic only: exact raw-hash matching
// is always required. Temporary preview provenance is separately checked by the capture registry.
// Explicit user aliases remain layout-wide and always have priority.
//
// Storage is shared with the ReplayLab Host (replaylab/mcp_host/src/shader_workspace/aliases.py),
// which publishes AI renames with source "ai". Keep the file format and the layout signature in
// sync with that module. One block layout has one file:
//   <selected project>/shared/dictionaries/cbuffer-aliases/<layoutSha256>.json  (project selected)
//   %LOCALAPPDATA%\ReplayLab\aliases\cbuffer\<layoutSha256>.json             (no project)
class ReplayLabCBufferAliases
{
  Q_DECLARE_TR_FUNCTIONS(ReplayLabCBufferAliases)

public:
  // The one alias root in scope: the selected project's share
  // (shared/dictionaries/cbuffer-aliases, it travels with the project folder) when a
  // project is selected, else the per-user root. See ReadCurrent for the one-time adoption.
  static QString StoreRoot();
  static QString ProjectStoreRoot();
  static QString UserStoreRoot();
  // "<block>\n<name>@<offset>\n..." over the top-level members; empty when it cannot be built.
  static QString LayoutSignature(const ConstantBlock &block);
  static QString LayoutHash(const ConstantBlock &block);
  static bool IsValidAlias(const QString &alias, QString *error = NULL);

  // (Re)reads the alias file of this block layout. Cheap enough to call on every panel refresh.
  // The replacement flag is retained for caller compatibility, never as cross-capture identity.
  void Load(const ConstantBlock *block, const QString &shaderRawSha256 = QString(),
            const QString &stage = QString(), qint64 bind = -1, qint64 space = -1,
            const QString &shaderResourceId = QString(),
            bool originalShaderIsReplaced = false);
  // Explicitly proven temporary projections inherit names, never source usage-review claims.
  void DiscardUsageReview() { m_UnusedComponents.clear(); }
  void UsePhysicalOffsetAliases() { m_PhysicalOffsetAliases = true; }
  void PreferUserAliases(const ReplayLabCBufferAliases &current);
  bool Loaded() const { return !m_LayoutSha256.isEmpty(); }
  QString LayoutSha256() const { return m_LayoutSha256; }

  ReplayLabCBufferAlias Resolve(const ReplayLabCBufferAliasKey &key) const;
  bool Set(const ReplayLabCBufferAliasKey &key, const QString &alias, QString *error);
  bool Remove(const ReplayLabCBufferAliasKey &key, QString *error);
  ReplayLabCBufferAlias BlockAlias() const;
  bool SetBlockAlias(const QString &alias, QString *error);
  bool RemoveBlockAlias(QString *error);
  const QList<ReplayLabCBufferSemanticField> &SemanticFields() const { return m_SemanticFields; }
  const QHash<qint64, QString> &UnusedComponents() const { return m_UnusedComponents; }

  // Modal rename dialog. Returns false when cancelled; an empty alias means "remove".
  static bool PromptAlias(QWidget *parent, const QString &original,
                          const ReplayLabCBufferAlias &current, QString &alias);
  static bool PromptBlockAlias(QWidget *parent, const QString &original,
                               const ReplayLabCBufferAlias &current, QString &alias);

private:
  struct ShaderScope
  {
    QString shaderRawSha256;
    QString shaderResourceId;
    QString stage;
    qint64 bind = -1;
    qint64 space = -1;
    QString layoutSha256;
  };
  struct Entry
  {
    QString path;
    qint64 offset = -1;
    QString alias;
    QString source;
    QString updatedAt;
    ShaderScope shaderScope;
  };
  struct BlockAliasEntry
  {
    ReplayLabCBufferAlias value;
    QString updatedAt;
    ShaderScope shaderScope;
  };
  struct Member
  {
    QString name;
    qint64 offset = -1;
  };

  static bool ReadFile(const QString &path, QList<Entry> &entries,
                       QList<BlockAliasEntry> &blockAliases, QString *error);
  static bool ReadCurrent(const QString &layoutSha256, QList<Entry> &entries,
                          QList<BlockAliasEntry> &blockAliases, QString *error);
  static bool ReadCurrentAt(const QString &storeRoot, const QString &layoutSha256,
                            QList<Entry> &entries, QList<BlockAliasEntry> &blockAliases,
                            QString *error);
  bool WriteFileTo(const QString &path, const QList<Entry> &entries,
                   const QList<BlockAliasEntry> &blockAliases, QString *error) const;
  static bool IsValidScope(const ShaderScope &scope);
  static ShaderScope ReadScope(const QJsonObject &object, const QString &layoutSha256);
  static void WriteScope(QJsonObject &object, const ShaderScope &scope);
  bool ScopeMatches(const ShaderScope &scope) const;
  bool EntryVisible(const Entry &entry) const;
  void RebuildLookup();
  bool Update(const ReplayLabCBufferAliasKey &key, const QString &alias, QString *error);
  bool UpdateBlockAlias(const QString &alias, QString *error);
  void LoadSemanticFields();

  QString m_LayoutSha256;
  QString m_BlockName;
  QList<Member> m_Members;
  QList<Entry> m_Entries;
  QHash<QString, int> m_PathLookup;
  QHash<qint64, int> m_OffsetLookup;
  bool m_HasPlaceholderMembers = false;
  bool m_PhysicalOffsetAliases = false;
  ShaderScope m_ShaderScope;
  QList<BlockAliasEntry> m_BlockAliases;
  QList<ReplayLabCBufferSemanticField> m_SemanticFields;
  QHash<qint64, QString> m_UnusedComponents;
  quint32 m_BlockByteSize = 0;
};

from __future__ import annotations

from pathlib import Path
from typing import Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _section(source: str, start: str, end: Optional[str] = None) -> str:
    begin = source.index(start)
    return source[begin:] if end is None else source[begin : source.index(end, begin)]


def test_native_composer_accepts_prompt_paste_and_composer_wide_file_drop() -> None:
    header = _source("qrenderdoc/Windows/AI/AIAssistant.h")
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    drop_frame = _section(source, "class AIAttachmentDropFrame", "class AIPromptEdit")
    prompt_edit = _section(source, "class AIPromptEdit", "struct ProviderCatalogText")
    constructor = _section(
        source,
        "AIAssistant::AIAssistant(",
        "AIAssistant::~AIAssistant()",
    )
    mime_ingest = _section(
        source,
        "bool AIAssistant::addMimeAttachments(",
        "void AIAssistant::removeComposerAttachment(",
    )
    chip_rebuild = _section(
        source,
        "void AIAssistant::rebuildAttachmentChips()",
        "void AIAssistant::appendSystem(",
    )

    assert "QPushButton *m_AddAttachment" in header
    assert "QVector<AIChatAttachment> m_ComposerAttachments" in header

    # The whole composer is a drop target. Accepting a proposed action only after the shared
    # attachment callback accepts the MIME data prevents Qt's text editor from silently inserting
    # file URLs as text. The prompt editor uses the identical callback for paste and direct drops.
    assert "class AIAttachmentDropFrame : public QFrame" in drop_frame
    assert "std::function<bool(const QMimeData *)> insertAttachments" in drop_frame
    assert "dragEnterEvent(QDragEnterEvent *event) override" in drop_frame
    assert "dragMoveEvent(QDragMoveEvent *event) override" in drop_frame
    assert "dropEvent(QDropEvent *event) override" in drop_frame
    assert "insertAttachments(event->mimeData())" in drop_frame
    assert "event->acceptProposedAction()" in drop_frame
    assert "insertFromMimeData(const QMimeData *source) override" in prompt_edit
    assert "insertAttachments(source)" in prompt_edit

    assert "AIAttachmentDropFrame *composer = new AIAttachmentDropFrame(mainPanel)" in constructor
    assert "m_AddAttachment = new QPushButton" in constructor
    assert "AssistantActionIcon::Add" in constructor
    assert 'tr("Add images or files")' in constructor
    assert "chooseAttachmentFiles" in constructor
    assert "composer->insertAttachments" in constructor
    assert "promptEdit->insertAttachments" in constructor
    assert "addMimeAttachments(mimeData)" in constructor

    assert "QFileDialog::getOpenFileNames" in source
    assert 'tr("All files (*)")' in source
    assert "mimeData->hasUrls()" in mime_ingest
    assert "url.toLocalFile()" in mime_ingest
    assert "addAttachmentFiles(paths)" in mime_ingest
    assert "mimeData->hasImage()" in mime_ingest
    assert "qvariant_cast<QImage>(mimeData->imageData())" in mime_ingest
    assert "qvariant_cast<QPixmap>(mimeData->imageData()).toImage()" in mime_ingest
    assert 'image.save(&output, "PNG")' in mime_ingest
    assert 'image.save(&output, "JPEG", 90)' in mime_ingest
    assert 'attachment.kind = lit("image")' in mime_ingest
    assert 'attachment.encoding = lit("base64")' in mime_ingest
    assert "QString::fromLatin1(bytes.toBase64())" in mime_ingest
    assert "m_ComposerAttachments" in chip_rebuild
    assert "removeComposerAttachment" in chip_rebuild
    assert "previewComposerAttachment" in chip_rebuild


def test_raw_clipboard_rasters_preserve_encoded_payloads_before_the_image_data_fallback() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    mime_ingest = _section(
        source,
        "bool AIAssistant::addMimeAttachments(",
        "void AIAssistant::removeComposerAttachment(",
    )

    # Windows and browsers commonly publish encoded raster data plus a flattened imageData
    # fallback. Inspect encoded MIME entries first so animation and the original codec survive;
    # GIF remains first only to avoid selecting a flattened sibling format ahead of it.
    raw_raster = mime_ingest[: mime_ingest.index("if(mimeData->hasImage())")]
    assert "mimeData->formats()" in raw_raster
    assert 'QLatin1String("image/gif")' in raw_raster
    assert "mimeData->data(" in raw_raster
    assert "MaxBinaryAttachmentBytes" in raw_raster
    assert "detectedMediaType" in raw_raster
    assert "detectedFormat" in raw_raster
    assert "rasterImageBytesHeaderIsSafe" in raw_raster
    assert "const bool qtRaster" in raw_raster
    assert "if(!qtRaster)" in raw_raster
    assert "qtRaster && providerNativeImageMediaType" in raw_raster
    assert 'lit("file")' in raw_raster
    assert "attachment.mediaType = detectedMediaType" in raw_raster
    assert "attachment.content = QString::fromLatin1(bytes.toBase64())" in raw_raster
    assert "requestInlineRasterThumbnail(attachment.id, bytes)" in raw_raster
    assert "AIFileThumbnail::decodeRasterData" not in raw_raster

    image_data_fallback = mime_ingest[mime_ingest.index("if(mimeData->hasImage())") :]
    assert 'image.save(&output, "PNG")' in image_data_fallback
    assert 'image.save(&output, "JPEG", 90)' in image_data_fallback
    assert mime_ingest.index("mimeData->formats()") < mime_ingest.index("mimeData->hasImage()")


def test_native_binary_validation_uses_canonical_base64_and_separate_8_mib_budget() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    validation = _section(
        source,
        "bool decodeCanonicalBase64(",
        "class AIPromptEdit",
    )
    composer_gate = _section(
        source,
        "bool AIAssistant::addComposerAttachment(",
        "void AIAssistant::chooseAttachmentFiles()",
    )
    file_ingest = _section(
        source,
        "bool AIAssistant::addAttachmentFiles(",
        "bool AIAssistant::addMimeAttachments(",
    )

    assert "const int MaxBinaryAttachmentBytes = 8 * 1024 * 1024" in source
    assert "const int MaxBinaryAttachmentTotalBytes = 8 * 1024 * 1024" in source
    assert "QByteArray::fromBase64(encoded)" in validation
    assert "bytes.toBase64() != encoded" in validation
    assert "bytes.size() > MaxBinaryAttachmentBytes" in validation
    assert "value.originalBytes != decodedBytes" in validation

    assert "totalBinaryBytes += existingBytes" in composer_gate
    assert "totalBinaryBytes > MaxBinaryAttachmentTotalBytes" in composer_gate
    assert "The attachments exceed the 8 MiB file limit for one request." in composer_gate

    # MIME is detected from bytes first, then falls back to a syntactically valid file MIME. An
    # image extension does not make the payload a Qt raster: content decoding decides that, while
    # an image-typed kind=file may be offered only to the separately bounded WIC/Shell path.
    assert "QMimeDatabase mimeDatabase" in file_ingest
    assert "mimeDatabase.mimeTypeForData(bytes)" in file_ingest
    assert "mimeDatabase.mimeTypeForFile(info)" in file_ingest
    assert "contentMediaType != QLatin1String(\"application/octet-stream\")" in file_ingest
    assert "rasterImageBytesHeaderIsSafe(" in file_ingest
    assert "const bool attachedAsImage" in file_ingest
    assert "validRasterImageMediaType(attachment.mediaType)" in file_ingest
    assert "providerNativeImageMediaType(attachment.mediaType)" in file_ingest
    assert 'lit("application/octet-stream")' in file_ingest


def test_mime_dimensions_and_qt_exif_transform_are_validated_around_raster_decode() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    view = _source("qrenderdoc/Windows/AI/AIConversationView.cpp")
    mime_validation = _section(
        source,
        "bool validAttachmentMediaType(",
        "QString attachmentMediaTypeEssence(",
    )
    reader_validation = _section(
        source,
        "bool rasterImageReaderHeaderIsSafe(",
        "bool decodedRasterImageIsSafe(",
    )
    decoded_validation = _section(
        source,
        "bool decodedRasterImageIsSafe(",
        "bool binaryImageHeaderIsSafe(",
    )
    binary_header = _section(
        source,
        "bool binaryImageHeaderIsSafe(",
        "bool rasterImageBytesHeaderIsSafe(",
    )
    thumbnail = _section(
        source,
        "QPixmap attachmentThumbnail(",
        "bool attachmentTextPreview(",
    )
    transcript_thumbnail = _section(
        view,
        "QImage conversationImageThumbnail(",
        "QString renderMessageAttachments(",
    )

    # MIME metadata is a bounded single-line token. Parameters are reduced to their lowercase
    # essence for comparisons, while malformed or control-character-bearing values are rejected.
    assert "mediaType.size() > 160" in mime_validation
    assert "mediaType != mediaType.trimmed()" in mime_validation
    assert "mediaType.contains(QChar::Null)" in mime_validation
    assert "mediaType.contains(QLatin1Char('\\r'))" in mime_validation
    assert "mediaType.contains(QLatin1Char('\\n'))" in mime_validation
    assert "validEssence" in mime_validation
    assert "section(QLatin1Char(';'), 0, 0).trimmed().toLower()" in source

    # Format sniffing and document rejection happen before reader.size(). Qt's auto-transform is
    # enabled before any decode so EXIF orientation is applied consistently. Header and decoded
    # pixels independently enforce dimension and aggregate-pixel ceilings.
    assert "reader.setDecideFormatFromContent(true)" in reader_validation
    assert "reader.setAutoTransform(true)" in reader_validation
    assert reader_validation.index("reader.format()") < reader_validation.index("reader.size()")
    assert "reader.canRead()" in reader_validation
    assert "size.width() > MaxImageDimension" in reader_validation
    assert "size.height() > MaxImageDimension" in reader_validation
    assert "qint64(size.width()) * qint64(size.height()) > MaxImagePixels" in reader_validation
    assert "image.width() <= MaxImageDimension" in decoded_validation
    assert "image.height() <= MaxImageDimension" in decoded_validation
    assert "qint64(image.width()) * qint64(image.height()) <= MaxImagePixels" in decoded_validation

    # The declared MIME cannot disagree with the detected codec for an inline Qt raster. The
    # canonical byte limit is checked before constructing the reader, and post-read validation is
    # applied in both the composer and persisted transcript thumbnail paths.
    assert "bytes.size() > MaxBinaryAttachmentBytes" in binary_header
    assert "validRasterImageMediaType(mediaType)" in binary_header
    assert "actualMediaType != attachmentMediaTypeEssence(mediaType)" in binary_header
    assert "decodedRasterImageIsSafe(image)" in thumbnail
    assert "reader.setAutoTransform(true)" in transcript_thumbnail
    assert "!detectedMediaType.isEmpty() && detectedMediaType == mediaType" in transcript_thumbnail
    assert "ConversationImageMaxDimension" in transcript_thumbnail
    assert "ConversationImageMaxPixels" in transcript_thumbnail
    assert "conversationDecodedRasterImageIsSafe(image)" in transcript_thumbnail


def test_dynamic_qt_raster_support_preserves_file_kind_compatibility_and_rejects_documents() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    view = _source("qrenderdoc/Windows/AI/AIConversationView.cpp")
    raster_validation = _section(
        source,
        "bool validRasterImageMediaType(",
        "bool attachmentsNeedV4(",
    )
    raster_formats = _section(
        source,
        "QString rasterImageMediaTypeForFormat(",
        "bool rasterImageReaderHeaderIsSafe(",
    )
    transcript_formats = _section(
        view,
        "QString conversationRasterMediaTypeForFormat(",
        "bool conversationDecodedRasterImageIsSafe(",
    )
    inline_classifier = _section(
        source,
        "bool isInlineRasterAttachment(",
        "bool decodeCanonicalBase64(",
    )
    file_ingest = _section(
        source,
        "bool AIAssistant::addAttachmentFiles(",
        "bool AIAssistant::addMimeAttachments(",
    )

    # "All image formats" means every raster codec actually installed in Qt, not a stale
    # source-code allow-list. Composer and transcript rendering must use the same policy, while
    # vector/document readers stay outside the untrusted in-process raster path.
    for format_mapping in (raster_formats, transcript_formats):
        assert "QImageReader::supportedImageFormats()" in format_mapping
        assert "supportedFormats.contains(normalized)" in format_mapping
        for unsafe_format in ("svg", "svgz", "pdf", "eps", "ps"):
            assert f'normalized == "{unsafe_format}"' in format_mapping
        assert 'lit("image/x-") + QString::fromLatin1(normalized)' in format_mapping
    assert "validRasterImageMediaType(fallback)" in raster_formats

    assert 'startsWith(QLatin1String("image/"))' in raster_validation
    for unsafe_media_type in (
        "image/svg+xml",
        "image/svg",
        "image/x-eps",
        "image/eps",
    ):
        assert unsafe_media_type in raster_validation
    assert "reader.setDecideFormatFromContent(true)" in raster_validation
    assert "reader.setAutoTransform(true)" in raster_validation
    assert raster_validation.index("reader.format()") < raster_validation.index("reader.size()")
    assert "MaxImageDimension" in raster_validation
    assert "MaxImagePixels" in raster_validation
    assert "reader.canRead()" in raster_validation

    # v4 used kind=image for provider-native codecs, but already persisted kind=file image
    # attachments remain valid. Non-native dynamically installed Qt codecs deliberately use file.
    assert 'attachment.kind == QLatin1String("image")' in inline_classifier
    assert 'attachment.kind == QLatin1String("file")' in inline_classifier
    assert 'attachment.encoding == QLatin1String("base64")' in inline_classifier
    assert "validRasterImageMediaType(attachment.mediaType)" in inline_classifier
    assert "providerNativeImageMediaType(attachment.mediaType)" in file_ingest
    assert "? lit(\"image\")" in file_ingest
    assert ": lit(\"file\")" in file_ingest


def test_bounded_dynamic_raster_animation_owns_the_original_encoded_payload() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    preview = _section(
        source,
        "void AIAssistant::previewComposerAttachment(",
        "void AIAssistant::appendSystem(",
    )
    # Keep the assertions behavioral: the implementation may regroup or rename its private
    # animation helpers without changing this contract.
    animation = source
    chip_rebuild = _section(
        source,
        "void AIAssistant::rebuildAttachmentChips()",
        "void AIAssistant::appendSystem(",
    )

    # Animation is discovered from the installed decoder rather than a GIF-only MIME branch. It
    # has byte, declared frame-count, and frame-count-times-canvas-pixel ceilings. The detected
    # format is passed to QMovie, so any compatible installed Qt codec that reports multiple frames
    # can share this path. Formats without a compatible Qt plugin use the separate WIC path.
    assert "#include <QMovie>" in source
    assert "reader.imageCount()" in animation
    assert "frames <= 1 || frames > MaxAnimatedImageFrames" in animation
    assert "MaxAnimatedImagePixelFrames" in animation
    assert "MaxPathReferenceAnimatedImageBytes" in animation
    assert "reader.format().toLower()" in animation
    assert "mediaType != attachmentMediaTypeEssence(attachment.mediaType)" in animation
    assert "new QMovie(buffer, format, parent)" in animation

    # QMovie does not own its input QIODevice. The movie owns the immutable buffer, retains only
    # the current decoded frame, and scales without changing the validated source dimensions.
    assert "buffer->setParent(movie)" in animation
    assert "movie->setCacheMode(QMovie::CacheNone)" in animation
    assert "movie->setScaledSize" in animation

    # The full-size preview and compact composer chip both use the same bounded movie path.
    assert "attachmentImageSize(attachment, &sourceSize)" in preview
    assert "AttachmentImagePreview *preview" in preview
    assert "attachmentPreviewPixmap(attachment)" in preview
    assert "preview->setMovie(movie, sourceSize)" in preview
    assert "preview->setPixmap(pixmap)" in preview
    assert "QMovie *movie" in chip_rebuild
    assert "QMovie::frameChanged" in chip_rebuild
    assert "movie->start()" in chip_rebuild
    # Titles, sources, trust notices, metadata, decode failures and binary notices are labels fed
    # by user-controlled names/MIME/source fields, so none may be interpreted as rich text.
    for label in ("title", "source", "trust", "metadata", "failure", "binaryNotice"):
        assert f"{label}->setTextFormat(Qt::PlainText)" in preview


def test_full_resolution_preview_starts_at_one_to_one_or_fit_and_supports_zoom_pan() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    canvas = _section(
        source,
        "class AttachmentImagePreview : public QWidget",
        "bool attachmentHasSafeImagePreview(",
    )
    preview = _section(
        source,
        "void AIAssistant::previewComposerAttachment(",
        "void AIAssistant::rebuildAttachmentChips()",
    )

    # Static images are decoded at their validated source resolution. Small images start at 1:1;
    # oversized images start fitted to the preview, and resize keeps that initial policy until the
    # user interacts.
    assert "attachmentPreviewPixmap(attachment)" in preview
    assert "reader.setScaledSize" not in _section(
        source,
        "QPixmap attachmentPreviewPixmap(",
        "class AttachmentImagePreview",
    )
    assert "m_Scale = qMin(qreal(1.0), fitScale())" in canvas
    assert "m_UserAdjusted" in canvas
    assert "resizeEvent(QResizeEvent *event) override" in canvas

    # Wheel zoom is anchored under the cursor and bounded; left-button drag pans while keeping a
    # sliver of the image visible. Animated GIF frames use the same canvas and interaction model.
    assert "wheelEvent(QWheelEvent *event) override" in canvas
    assert "event->angleDelta().y()" in canvas
    assert "std::pow(1.2" in canvas
    assert "sourcePosition" in canvas
    assert "qreal(16.0)" in canvas
    assert "mousePressEvent(QMouseEvent *event) override" in canvas
    assert "mouseMoveEvent(QMouseEvent *event) override" in canvas
    assert "mouseReleaseEvent(QMouseEvent *event) override" in canvas
    assert "m_Pan += position - m_LastMousePosition" in canvas
    assert "Qt::ClosedHandCursor" in canvas
    assert "constrainPan()" in canvas
    assert "QMovie::frameChanged" in canvas

    # Regression guard for the former fixed preview raster that destroyed original resolution.
    assert "QSize(1200, 900)" not in preview
    assert "safeAttachmentMovie(attachment, sourceSize, preview)" in preview


def test_unicode_text_files_and_content_thumbnails_use_bounded_strict_decoding() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    text_media_types = _section(
        source,
        "bool validTextAttachmentMediaType(",
        "bool isTextAttachment(",
    )
    decoder = _section(
        source,
        "bool decodeSupportedTextBytes(",
        "bool attachmentDecodedBytes(",
    )
    file_ingest = _section(
        source,
        "bool AIAssistant::addAttachmentFiles(",
        "bool AIAssistant::addMimeAttachments(",
    )
    text_preview = _section(
        source,
        "bool attachmentTextPreview(",
        "QPixmap textAttachmentThumbnail(",
    )
    text_thumbnail = _section(
        source,
        "QPixmap textAttachmentThumbnail(",
        "bool attachmentImageSize(",
    )
    preview = _section(
        source,
        "void AIAssistant::previewComposerAttachment(",
        "void AIAssistant::rebuildAttachmentChips()",
    )

    # Unmarked data is strict UTF-8 by default. A Unicode BOM may select UTF-16/UTF-32 without
    # being mistaken for binary because of embedded NUL bytes. The Windows locale codec is tried
    # only after MIME/type evidence has classified a user-selected file as text.
    assert 'QTextCodec::codecForName("UTF-8")' in decoder
    assert "QTextCodec::codecForUtfText(bytes, utf8)" in decoder
    assert "codec == utf8 && bytes.contains('\\0')" in decoder
    assert "QTextCodec::ConverterState state" in decoder
    assert "state.invalidChars == 0" in decoder
    assert "QChar::ByteOrderMark" in decoder
    assert "decoded.contains(QChar::Null)" in decoder
    assert "decoded.size() > maximumCharacters" in decoder
    assert "allowLocalEncoding = false" in decoder
    assert "!valid && allowLocalEncoding && codec == utf8" in decoder
    assert "QTextCodec::codecForLocale()" in decoder

    # Text is content-detected and not restricted to a short extension list: every text/* MIME,
    # structured JSON/XML variants, and common source/config application types remain eligible.
    assert 'normalized.startsWith(QLatin1String("text/"))' in text_media_types
    assert 'normalized.endsWith(QLatin1String("+json"))' in text_media_types
    assert 'normalized.endsWith(QLatin1String("+xml"))' in text_media_types
    for textual_type in (
        "application/json",
        "application/xml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/sql",
        "application/graphql",
    ):
        assert textual_type in text_media_types

    assert "decodeSupportedTextBytes(bytes, &contents," in file_ingest
    assert "validTextAttachmentMediaType(mediaType))" in file_ingest
    assert "!bytes.contains('\\0')" not in file_ingest
    assert "attachment.content = contents" in file_ingest
    assert "attachment.originalBytes = contents.toUtf8().size()" in file_ingest

    # Content previews are bounded and plain text. File references are checked before and after a
    # bounded read so a changed path can neither be shown nor accidentally persisted as content.
    assert "MaxTextThumbnailReadBytes" in text_preview
    assert "decodeCanonicalBase64" in text_preview
    assert "decodeSupportedTextBytes" in text_preview
    assert text_preview.count("pathReferenceMatchesCurrentFile(attachment)") >= 2
    assert "MaxTextThumbnailChars" in text_preview
    assert "QFontDatabase::systemFont(QFontDatabase::FixedFont)" in text_thumbnail
    assert "QPainter" in text_thumbnail
    assert "metrics.elidedText" in text_thumbnail
    assert "painter.drawText" in text_thumbnail
    assert "contents->setPlainText" in preview
    assert "attachmentTextPreview(*selected" in preview


def test_system_thumbnails_are_async_composer_only_and_unknown_files_keep_named_fallbacks() -> None:
    header = _source("qrenderdoc/Windows/AI/AIAssistant.h")
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    shell_header = _source("qrenderdoc/Windows/AI/AIFileThumbnail.h")
    shell_source = _source("qrenderdoc/Windows/AI/AIFileThumbnail.cpp")
    thumbnail_request = _section(
        source,
        "void AIAssistant::requestSystemThumbnail(",
        "bool AIAssistant::addAttachmentFiles(",
    )
    inline_thumbnail_request = _section(
        source,
        "void AIAssistant::requestInlineRasterThumbnail(",
        "bool AIAssistant::addAttachmentFiles(",
    )
    file_ingest = _section(
        source,
        "bool AIAssistant::addAttachmentFiles(",
        "bool AIAssistant::addMimeAttachments(",
    )
    remove = _section(
        source,
        "void AIAssistant::removeComposerAttachment(",
        "void AIAssistant::previewComposerAttachment(",
    )
    preview = _section(
        source,
        "void AIAssistant::previewComposerAttachment(",
        "void AIAssistant::rebuildAttachmentChips()",
    )
    chip_rebuild = _section(
        source,
        "void AIAssistant::rebuildAttachmentChips()",
        "void AIAssistant::appendSystem(",
    )
    serializer = _section(
        source,
        "QJsonObject chatAttachmentToJSON(",
        "QJsonArray chatAttachmentsToJSON(",
    )
    persistence = _section(
        source,
        "QJsonObject AIAssistant::conversationToJSON(",
        "void AIAssistant::scheduleConversationSave(",
    )
    decode_pool = _section(
        source,
        "QThreadPool *aiBackgroundThreadPool()",
        "struct AIConversationSaveWork",
    )

    # Windows Shell supplies true content thumbnails for installed providers (video, PDF, office
    # documents, etc.). THUMBNAILONLY deliberately refuses generic file-type icons. The request
    # and returned bitmap are both bounded, and native resources have scoped release paths.
    assert "loadSystemThumbnail" in shell_header
    assert "IShellItemImageFactory" in shell_source
    assert "SIIGBF_THUMBNAILONLY" in shell_source
    assert "SIIGBF_ICONONLY" not in shell_source
    assert "const int MaxThumbnailEdge = 512" in shell_source
    assert "MaxThumbnailPixels" in shell_source
    assert "bounded.scale(MaxThumbnailEdge, MaxThumbnailEdge, Qt::KeepAspectRatio)" in shell_source
    assert "width64 > request.width() || height64 > request.height()" in shell_source
    assert "COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE" in shell_source
    assert "m_uninitialize = SUCCEEDED(result)" in shell_source
    assert "CoUninitialize()" in shell_source
    assert "m_factory->Release()" in shell_source
    assert "DeleteObject(m_bitmap)" in shell_source
    assert "ReleaseDC(NULL, screen)" in shell_source
    assert "GetDIBits" in shell_source

    # File-backed WIC and Shell thumbnail work requested here runs off the GUI thread. A weak owner
    # gates the result, file metadata is snapshotted and revalidated around decode, and the GUI
    # insert is queued and accepted only for an attachment that is still present. Raster-typed
    # files try bounded WIC GIF animation, then a static WIC decode, then Shell, without requiring
    # a foreign Qt plugin.
    assert "bool preferRasterDecoder = false" in header
    assert "QPointer<AIAssistant> assistant(this)" in thumbnail_request
    assert "new QThreadPool" in decode_pool
    assert "setMaxThreadCount(1)" in decode_pool
    assert "QThread::LowPriority" in source
    assert "aiBackgroundThreadPool()->start" in thumbnail_request
    assert "QThreadPool::globalInstance()->start" not in thumbnail_request
    assert "fileInfoIsSafeReferenceTarget(before, &currentCanonicalPath)" in thumbnail_request
    assert "before.size() != expectedBytes" in thumbnail_request
    assert "before.lastModified().toMSecsSinceEpoch() != expectedModifiedUnixMs" in thumbnail_request
    assert "preferRasterDecoder && expectedBytes <= MaxPathReferenceAnimatedImageBytes" in thumbnail_request
    assert "AIFileThumbnail::decodeGIFAnimationFile" in thumbnail_request
    assert "preferRasterDecoder && expectedBytes <= MaxPathReferenceImagePreviewBytes" in thumbnail_request
    assert "AIFileThumbnail::decodeRasterFile" in thumbnail_request
    assert "AIFileThumbnail::loadSystemThumbnail(canonicalPath, QSize(96, 96))" in thumbnail_request
    assert thumbnail_request.index("AIFileThumbnail::decodeGIFAnimationFile") < (
        thumbnail_request.index("AIFileThumbnail::decodeRasterFile")
    ) < thumbnail_request.index("AIFileThumbnail::loadSystemThumbnail")
    assert "fileInfoIsSafeReferenceTarget(after, &finalCanonicalPath)" in thumbnail_request
    assert "after.size() != expectedBytes" in thumbnail_request
    assert "after.lastModified().toMSecsSinceEpoch() != expectedModifiedUnixMs" in thumbnail_request
    assert "QTimer::singleShot" in thumbnail_request
    assert "attachment.id != attachmentId" in thumbnail_request
    assert "pathReferenceMatchesCurrentFile(attachment)" in thumbnail_request
    assert "m_ComposerSystemThumbnails.insert(attachmentId, thumbnail)" in thumbnail_request
    assert "m_ComposerSystemAnimations.insert(attachmentId, cachedAnimation)" in thumbnail_request
    assert "rebuildAttachmentChips()" in thumbnail_request

    # Inline kind=file raster MIME data uses the same background WIC policy without retaining its
    # original local path. The callback rechecks id/kind/encoding/byte count before caching pixels.
    assert "void requestInlineRasterThumbnail" in header
    assert "encodedBytes.size() > MaxBinaryAttachmentBytes" in inline_thumbnail_request
    assert "QPointer<AIAssistant> assistant(this)" in inline_thumbnail_request
    assert "aiBackgroundThreadPool()->start" in inline_thumbnail_request
    assert "QThreadPool::globalInstance()->start" not in inline_thumbnail_request
    assert "AIFileThumbnail::decodeGIFAnimationData" in inline_thumbnail_request
    assert "AIFileThumbnail::decodeRasterData" in inline_thumbnail_request
    assert inline_thumbnail_request.index("AIFileThumbnail::decodeGIFAnimationData") < (
        inline_thumbnail_request.index("AIFileThumbnail::decodeRasterData")
    )
    assert "QTimer::singleShot" in inline_thumbnail_request
    for current_attachment_guard in (
        "attachment.id != attachmentId",
        'attachment.kind != QLatin1String("file")',
        'attachment.encoding != QLatin1String("base64")',
        "attachment.originalBytes != expectedBytes",
    ):
        assert current_attachment_guard in inline_thumbnail_request
    assert "m_ComposerSystemThumbnails.insert(attachmentId, thumbnail)" in inline_thumbnail_request
    assert "m_ComposerSystemAnimations.insert(attachmentId, cachedAnimation)" in inline_thumbnail_request

    # A content thumbnail is composer-only state. This covers both inline files (whose local path
    # is discarded after the async request) and large path references without granting history a
    # fresh read. File ingestion schedules work; it never calls the Shell provider synchronously.
    assert "QHash<QString, QImage> m_ComposerSystemThumbnails" in header
    assert "QHash<QString, ComposerRasterAnimation> m_ComposerSystemAnimations" in header
    assert "requestSystemThumbnail(" in file_ingest
    assert "validRasterImageMediaType(attachment.mediaType)" in file_ingest
    assert "AIFileThumbnail::loadSystemThumbnail" not in file_ingest
    assert "m_ComposerSystemThumbnails.remove(item.id)" in remove
    assert "m_ComposerSystemAnimations.remove(item.id)" in remove
    assert 'QLatin1String("pairId")' in remove
    assert "m_ComposerSystemThumbnails" not in serializer
    assert "m_ComposerSystemAnimations" not in serializer
    assert 'row[lit("systemThumbnail")]' not in persistence
    assert 'object[lit("systemThumbnail")]' not in persistence
    assert 'row[lit("systemAnimation")]' not in persistence
    assert 'object[lit("systemAnimation")]' not in persistence
    assert "systemThumbnail" not in serializer
    assert "systemAnimation" not in serializer

    # Only successfully decoded/generated content gets the compact thumbnail-only button. The
    # filename remains available to assistive tech and the tooltip, while an unsupported file uses
    # the ordinary named card instead of a misleading generic image tile.
    assert "attachmentThumbnail(attachment" in chip_rebuild
    assert "attachmentTextPreview(attachment" in chip_rebuild
    assert "textAttachmentThumbnail(" in chip_rebuild
    assert "m_ComposerSystemThumbnails.value(attachment.id)" in chip_rebuild
    assert "m_ComposerSystemAnimations.value(attachment.id)" in chip_rebuild
    assert "AttachmentFrameAnimation" in chip_rebuild
    thumbnail_button = chip_rebuild.index("QToolButton *preview = new QToolButton(chip)")
    fallback_button = chip_rebuild.index(
        "QPushButton *preview = new QPushButton(attachment.title, chip)", thumbnail_button
    )
    assert thumbnail_button < fallback_button
    thumbnail_branch = chip_rebuild[thumbnail_button:fallback_button]
    assert "setIcon" in thumbnail_branch
    assert "attachment.title" in thumbnail_branch
    assert "setToolTip" in thumbnail_branch
    assert "setAccessibleName" in thumbnail_branch
    assert "setText(attachment.title)" not in thumbnail_branch
    assert 'type->setText(tr("Image"))' not in chip_rebuild
    assert "previewComposerAttachment(attachment.id)" in chip_rebuild

    # A Shell thumbnail can be enlarged in the same safe viewer without pretending the underlying
    # arbitrary file is a decodable image. The fallback path still explains binary contents.
    assert "m_ComposerSystemThumbnails.value(selected->id)" in preview
    assert "AttachmentImagePreview" in preview
    assert "Binary file contents are not rendered in the preview." in preview


def test_wic_full_image_preview_is_async_guarded_and_falls_back_to_the_thumbnail() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    # These invariants intentionally span private helpers; do not couple the contract to their
    # current names or layout.
    system_raster = source
    preview = _section(
        source,
        "void AIAssistant::previewComposerAttachment(",
        "void AIAssistant::rebuildAttachmentChips()",
    )

    # Compatibility kind=file payloads and bounded current file references can use WIC. Inline
    # bytes remain canonical base64, and a path reference is checked both by the WIC file helper
    # and once more by the attachment layer after decoding.
    assert 'attachment.kind == QLatin1String("file")' in system_raster
    assert 'attachment.encoding == QLatin1String("base64")' in system_raster
    assert "decodeCanonicalBase64(attachment.content, &bytes)" in system_raster
    assert "attachment.originalBytes <= MaxPathReferenceImagePreviewBytes" in system_raster
    assert "pathReferenceMatchesCurrentFile(attachment)" in system_raster
    assert "AIFileThumbnail::decodeRasterData" in system_raster
    assert "AIFileThumbnail::decodeRasterFile" in system_raster
    assert "AIFileThumbnail::decodeGIFAnimationData" in system_raster
    assert "AIFileThumbnail::decodeGIFAnimationFile" in system_raster
    assert "MaxPathReferenceAnimatedImageBytes" in system_raster
    assert system_raster.count("pathReferenceMatchesCurrentFile(attachment)") >= 2

    # Full WIC decode happens on the dedicated single-lane low-priority AI pool. The modal dialog
    # stays responsive with a plain loading label, every QObject crossing the worker boundary is
    # weakly guarded, and the result is installed on the GUI thread. Failure uses the
    # already-bounded thumbnail or a safe notice.
    assert 'tr("Loading image preview...")' in preview
    assert "loading->setTextFormat(Qt::PlainText)" in preview
    for guard in (
        "QPointer<AIAssistant>",
        "QPointer<QDialog>",
        "QPointer<AttachmentImagePreview>",
        "QPointer<QLabel>",
    ):
        assert guard in preview
    assert "aiBackgroundThreadPool()->start" in preview
    assert "QThreadPool::globalInstance()->start" not in preview
    assert "attachmentSystemRasterAnimation(attachment)" in preview
    assert "attachmentSystemRasterPreview(attachment)" in preview
    assert preview.index("attachmentSystemRasterAnimation(attachment)") < preview.index(
        "attachmentSystemRasterPreview(attachment)"
    )
    assert "QTimer::singleShot" in preview
    assert "pathReferenceMatchesCurrentFile(attachment)" in preview
    assert "AttachmentFrameAnimation *player" in preview
    assert "animation.frames" in preview
    assert "animation.frameDelaysMs" in preview
    assert "setRasterAnimationFrame" in preview
    assert "image = fallbackThumbnail" in preview
    assert '"The image preview could not be decoded safely."' in preview
    assert "loadingGuard->hide()" in preview
    assert "previewGuard->show()" in preview


def test_wic_raster_helper_has_bounded_format_exif_and_file_snapshot_contracts() -> None:
    header = _source("qrenderdoc/Windows/AI/AIFileThumbnail.h")
    source = _source("qrenderdoc/Windows/AI/AIFileThumbnail.cpp")
    project = _source("qrenderdoc/qrenderdoc_local.vcxproj")
    qmake = _source("qrenderdoc/qrenderdoc.pro")
    data_entry = _section(
        source,
        "RasterDecodeResult decodeRasterData(",
        "RasterDecodeResult decodeRasterFile(",
    )
    file_entry = _section(source, "RasterDecodeResult decodeRasterFile(")

    # The helper exposes pixels plus raw/oriented dimensions and EXIF orientation. Byte limits are
    # explicit at the public boundary, and both APIs remain a null-result no-op off Windows.
    for declaration in (
        "QImage image;",
        "QSize sourceSize;",
        "QSize orientedSourceSize;",
        "int orientation = 1;",
        "decodeRasterData(const QByteArray &encodedData",
        "decodeRasterFile(const QString &absolutePath",
        "64LL * 1024LL * 1024LL",
        "256LL * 1024LL * 1024LL",
    ):
        assert declaration in header
    for non_windows_entry in (data_entry, file_entry):
        assert "#else" in non_windows_entry
        assert "Q_UNUSED" in non_windows_entry
        assert "return RasterDecodeResult();" in non_windows_entry
    assert "Windows/AI/AIFileThumbnail.cpp" in qmake
    assert 'Windows\\AI\\AIFileThumbnail.cpp' in project

    # WIC is reached only after bounded input checks. Payload signatures, file extensions, and the
    # registered decoder's MIME/extension claims independently reject the listed known
    # document/vector formats; this is not a universal classifier for third-party codecs.
    assert "HardMaxEncodedBytes = 512LL * 1024LL * 1024LL" in source
    assert "byteCount > 0 && byteCount <= maximumEncodedBytes" in source
    assert "std::numeric_limits<DWORD>::max" in source
    for extension in ("svg", "svgz", "pdf", "ps", "eps", "ai", "wmf", "emf"):
        assert f'QStringLiteral("{extension}")' in source
    for signature in (
        "quint8(prefix[0]) == 0x1f",
        'probe.startsWith("%pdf-")',
        'probe.startsWith("%!ps")',
        'probe.startsWith("%!adobe")',
        'indexOf("<svg")',
        "quint8(prefix[0]) == 0xd7",
    ):
        assert signature in source
    for blocked_media_type in (
        "image/svg+xml",
        "application/pdf",
        "application/postscript",
        "image/wmf",
        "image/x-wmf",
        "image/emf",
        "image/x-emf",
    ):
        assert blocked_media_type in source
    assert "FAILED(decoder->GetDecoderInfo(info.address())) || !info" in source
    assert "mimeTypes.isEmpty() && extensions.isEmpty()" in source

    # Source dimensions and decoded allocation are bounded separately. maximumSize never upscales:
    # scaling occurs only when an oriented source dimension exceeds the requested maximum.
    assert "MaxRasterDimension = 16384" in source
    assert "MaxRasterPixels = 64LL * 1024LL * 1024LL" in source
    assert "quint64(width) * quint64(height)" in source
    assert "orientedSize.width() > maximumSize.width()" in source
    assert "orientedSize.height() > maximumSize.height()" in source
    assert "orientedSize.scale(maximumSize, Qt::KeepAspectRatio)" in source
    assert "WICBitmapInterpolationModeFant" in source
    assert "stride64 > quint64((std::numeric_limits<UINT>::max)())" in source
    assert "byteCount64 > quint64((std::numeric_limits<UINT>::max)())" in source
    assert "GUID_WICPixelFormat32bppBGRA" in source
    assert "GUID_WICPixelFormat32bppPBGRA" in source

    # EXIF orientation accepts only 1..8 from the common metadata locations, swaps dimensions for
    # 5..8, applies every mirror/rotation case, and verifies the final oriented pixel dimensions.
    for query in (
        'L"/app1/ifd/{ushort=274}"',
        'L"/ifd/{ushort=274}"',
        'L"/xmp/tiff:Orientation"',
    ):
        assert query in source
    assert "orientation >= 5 && orientation <= 8" in source
    orientation_transform = _section(source, "switch(orientation)", "QImage copyWICPixels(")
    for transform in (
        "case 2: return source.mirrored(true, false);",
        "case 3: return source.transformed(QTransform().rotate(180.0));",
        "case 4: return source.mirrored(false, true);",
        "case 5:",
        "source.transformed(QTransform().rotate(90.0)).mirrored(true, false)",
        "case 6: return source.transformed(QTransform().rotate(90.0));",
        "case 7:",
        "source.transformed(QTransform().rotate(90.0)).mirrored(false, true)",
        "case 8: return source.transformed(QTransform().rotate(270.0));",
    ):
        assert transform in orientation_transform
    assert "result.image.size() != decodedOrientedSize" in source

    # InitializeFromMemory borrows the input storage, so the public data entry pins a COW copy for
    # the synchronous decode. COM resources are scoped and apartment uninitialization is balanced.
    assert "const QByteArray pinnedData = encodedData" in source
    assert "InitializeFromMemory" in source
    assert "m_pointer->Release()" in source
    assert "m_pointer = NULL" in source
    assert "COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE" in source
    assert "m_usable = SUCCEEDED(result) || result == RPC_E_CHANGED_MODE" in source
    assert "m_uninitialize = SUCCEEDED(result)" in source

    # File decode admits an absolute canonical, non-QFileInfo-symlink target on an explicitly local
    # drive type. It snapshots size and mtime before WIC and repeats canonical/size/mtime/symlink
    # checks afterward. This still does not claim handle/file-ID pinning or all reparse semantics.
    assert "selectedFile.isAbsolute()" in source
    assert "selectedFile.exists()" in source
    assert "selectedFile.isFile()" in source
    assert "selectedFile.canonicalFilePath()" in source
    assert "nativePathIsLocal(nativePath)" in source
    assert "GetDriveTypeW" in source
    for local_drive_type in ("DRIVE_FIXED", "DRIVE_REMOVABLE", "DRIVE_RAMDISK", "DRIVE_CDROM"):
        assert local_drive_type in source
    assert "selectedFile.isSymLink()" in source
    assert "originalFile.isSymLink()" in source
    assert "const qint64 originalSize" in source
    assert "const qint64 originalModifiedMs" in source
    assert "prefixFile.size() != originalSize" in source
    assert "currentFile.refresh()" in source
    assert "currentFile.isSymLink()" in source
    assert "currentFile.canonicalFilePath() != canonicalPath" in source
    assert "currentFile.size() != originalSize" in source
    assert "currentFile.lastModified().toMSecsSinceEpoch() != originalModifiedMs" in source


def test_wic_gif_animation_is_bounded_composed_and_played_without_qt_plugins() -> None:
    header = _source("qrenderdoc/Windows/AI/AIFileThumbnail.h")
    source = _source("qrenderdoc/Windows/AI/AIFileThumbnail.cpp")
    assistant = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    data_entry = _section(
        source,
        "RasterAnimationResult decodeGIFAnimationData(",
        "RasterAnimationResult decodeGIFAnimationFile(",
    )
    file_entry = _section(source, "RasterAnimationResult decodeGIFAnimationFile(")
    player = _section(
        assistant,
        "class AttachmentFrameAnimation : public QObject",
        "bool attachmentHasSafeImagePreview(",
    )

    # WIC animation is deliberately GIF-specific; other multi-page raster formats remain static.
    # The public result requires at least two equally timed frames and a valid composed canvas.
    for declaration in (
        "struct RasterAnimationResult",
        "QVector<QImage> frames;",
        "QVector<int> frameDelaysMs;",
        "QSize sourceSize;",
        "frames.size() < 2",
        "frameDelaysMs.size() != frames.size()",
        "decodeGIFAnimationData(",
        "decodeGIFAnimationFile(",
        "maximumFrames = 2048",
        "maximumPixelFrames = 32LL * 1024LL * 1024LL",
    ):
        assert declaration in header
    assert 'prefix.startsWith("GIF87a") || prefix.startsWith("GIF89a")' in source

    # Encoded bytes, frame count, composed canvas pixel-frames, and requested dimensions all have
    # independent ceilings. Scaling is proportional and occurs only when the source exceeds bounds.
    assert "maximumFrames < 2 || maximumPixelFrames <= 0" in source
    assert "frameCount < 2" in source
    assert "frameCount > UINT(maximumFrames)" in source
    assert "targetCanvas.width() > maximumSize.width()" in source
    assert "targetCanvas.height() > maximumSize.height()" in source
    assert "targetCanvas.scale(maximumSize, Qt::KeepAspectRatio)" in source
    assert "qint64(targetCanvas.width()) * qint64(targetCanvas.height()) * qint64(frameCount)" in source
    assert "maximumPixelFrames" in source

    # WIC subframes must stay on the logical GIF canvas and use orientation 1. Disposal 2 clears
    # the prior rectangle; disposal 3 restores a copied canvas; frames then compose SourceOver.
    assert "sourceRect.right() >= sourceCanvas.width()" in source
    assert "sourceRect.bottom() >= sourceCanvas.height()" in source
    assert "frameOrientation(frame.get()) != 1" in source
    assert "previousDisposal == 2" in source
    assert "QPainter::CompositionMode_Source" in source
    assert "previousDisposal == 3" in source
    assert "canvas = previousRestoreCanvas" in source
    assert "QPainter::CompositionMode_SourceOver" in source
    assert "canvas.copy()" in source

    # Missing delays default to 100 ms; supplied delays are clamped to a responsive 20 ms..10 s.
    # The UI player is single-shot per frame, advances modulo the validated frame vector, and
    # repeats the same clamp before scheduling each tick.
    assert "delayMs64 == 0 ? 100" in source
    assert "qBound<quint64>(20, delayMs64, 10000)" in source
    assert "m_Timer.setSingleShot(true)" in player
    assert "m_Frames.size() < 2" in player
    assert "m_FrameDelaysMs.size() != m_Frames.size()" in player
    assert "m_Index = (m_Index + 1) % m_Frames.size()" in player
    assert player.count("qBound(20, m_FrameDelaysMs[") == 2

    # Data storage remains alive for WIC composition. File animation accepts only a current local
    # non-symlink GIF snapshot and repeats canonical/size/mtime/symlink checks after decoding.
    assert "const QByteArray pinnedData = encodedData" in data_entry
    assert "selectedFile.isSymLink()" in file_entry
    assert "nativePathIsLocal(nativePath)" in file_entry
    assert "prefixFile.size() != originalSize" in file_entry
    assert "!isGIFPayload(prefix)" in file_entry
    assert "currentFile.refresh()" in file_entry
    assert "currentFile.isSymLink()" in file_entry
    assert "currentFile.canonicalFilePath() != canonicalPath" in file_entry
    assert "currentFile.size() != originalSize" in file_entry
    assert "currentFile.lastModified().toMSecsSinceEpoch() != originalModifiedMs" in file_entry
    for non_windows_entry in (data_entry, file_entry):
        assert "#else" in non_windows_entry
        assert "Q_UNUSED" in non_windows_entry
        assert "return RasterAnimationResult();" in non_windows_entry


def test_attachments_are_typed_untrusted_fields_not_prompt_concatenation() -> None:
    header = _source("qrenderdoc/Windows/AI/AIConversationView.h")
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    serializer = _section(
        source,
        "QJsonObject chatAttachmentToJSON(",
        "QJsonArray chatAttachmentsToJSON(",
    )
    envelope = _section(
        source,
        "QString AIAssistant::conversationPrompt(",
        "void AIAssistant::advanceChatContextFreeze()",
    )

    assert "struct AIChatAttachment" in header
    assert "QVector<AIChatAttachment> attachments" in header
    assert 'object[lit("content")] = attachment.content' in serializer
    assert 'object[lit("trust")] = lit("untrusted")' in serializer

    # The user's request and attachment payloads occupy distinct JSON fields. In particular,
    # selected source text must never be appended to the request string where it could be
    # mistaken for higher-authority instructions.
    assert 'request[lit("content")] = prompt' in envelope
    assert 'request[lit("attachments")]' in envelope
    assert "chatAttachmentsToJSON(attachments)" in envelope
    assert 'message[lit("content")] = item->text' in envelope
    assert 'message[lit("attachments")]' in envelope
    assert "chatAttachmentsToJSON(item->attachments)" in envelope
    assert "prompt += attachment.content" not in envelope
    assert "prompt + attachment.content" not in envelope


def test_v5_is_negotiated_for_context_and_paths_while_v4_binary_and_v3_text_remain_compatible() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    schema_gate = _section(
        source,
        "bool attachmentsNeedV4(",
        "QJsonObject chatAttachmentToJSON(",
    )
    envelope = _section(
        source,
        "QString AIAssistant::conversationPrompt(",
        "void AIAssistant::advanceChatContextFreeze()",
    )
    submit = _section(
        source,
        "void AIAssistant::startChat(",
        "void AIAssistant::startNextChat()",
    )
    launch = _section(
        source,
        "void AIAssistant::launchChat(",
        "void AIAssistant::handleChatProcessStarted(",
    )

    assert 'attachment.kind == lit("image")' in source
    assert 'attachment.kind == lit("file")' in source
    assert 'attachment.kind == QLatin1String("file-ref")' in source
    assert "attachmentRequiresV4(attachment)" in schema_gate
    assert "attachmentRequiresV5(attachment)" in schema_gate
    assert "isPathReferenceAttachment(attachment)" in source
    assert "!attachment.context.isEmpty()" in source
    assert 'schemas.contains(lit("replaylab-conversation/v5"))' in schema_gate
    assert "if(attachmentsNeedV5(attachments))" in schema_gate
    assert 'schemas.contains(lit("replaylab-conversation/v4"))' in schema_gate
    assert "if(attachmentsNeedV4(attachments))" in schema_gate
    assert 'schemas.contains(lit("replaylab-conversation/v3"))' in schema_gate

    assert 'inputSchemas.contains(lit("replaylab-conversation/v5"))' in envelope
    assert 'inputSchemas.contains(lit("replaylab-conversation/v4"))' in envelope
    assert 'inputSchemas.contains(lit("replaylab-conversation/v3"))' in envelope
    assert 'inputSchemas.contains(lit("replaylab-conversation/v2"))' in envelope
    assert 'lit("replaylab-conversation/v5")' in envelope
    assert 'lit("replaylab-conversation/v4")' in envelope
    assert 'lit("replaylab-conversation/v3")' in envelope
    assert "supportsV5 ?" in envelope
    assert "schemaSupportsAttachments(inputSchemas, item->attachments)" in envelope

    for guarded_path in (submit, launch):
        assert "schemaSupportsAttachments" in guarded_path
        assert "attachmentsNeedV5" in guarded_path
        assert "attachmentsNeedV4" in guarded_path
        assert "does not support image or file attachments" in guarded_path
        assert "does not support structured attachments" in guarded_path
    assert "failBeforeStart" in launch
    assert "request.includeConversation || !request.attachments.isEmpty()" in launch
    assert "request.clarificationResponse, request.attachments" in launch


def test_message_and_pending_queue_persistence_report_omitted_attachments() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    message_save = _section(
        source,
        "QJsonObject AIAssistant::conversationToJSON(",
        "bool AIAssistant::conversationFromJSON(",
    )
    message_load = _section(
        source,
        "bool AIAssistant::conversationFromJSON(",
        "void AIAssistant::loadConversations()",
    )
    queue_store = _section(
        source,
        "void AIAssistant::saveConversations()",
        "void AIAssistant::scheduleConversationSave()",
    )
    queue_load = _section(
        source,
        "void AIAssistant::loadConversations()",
        "void AIAssistant::saveConversations()",
    )

    for writer in (message_save, queue_store):
        assert 'row[lit("attachments")]' in writer
        assert 'row[lit("attachmentsOmitted")]' in writer
        assert "chatAttachmentsToJSON" in writer
    for reader in (message_load, queue_load):
        assert 'row.value(lit("attachments"))' in reader
        assert 'row.value(lit("attachmentsOmitted"))' in reader
        assert "chatAttachmentsFromJSON" in reader
    assert 'lit("replaylab-ai-conversations/v5")' in queue_store
    assert 'storeSchema != lit("replaylab-ai-conversations/v2")' in queue_load
    assert 'storeSchema != lit("replaylab-ai-conversations/v3")' in queue_load
    assert 'storeSchema != lit("replaylab-ai-conversations/v4")' in queue_load
    assert 'storeSchema != lit("replaylab-ai-conversations/v5")' in queue_load
    assert "Attachment content unavailable" in message_load
    assert "were not persisted" in queue_load


def test_v5_context_and_path_reference_fields_round_trip_without_reopening_history() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    view = _source("qrenderdoc/Windows/AI/AIConversationView.cpp")
    serializer = _section(
        source,
        "QJsonObject chatAttachmentToJSON(",
        "QJsonArray chatAttachmentsToJSON(",
    )
    loader = _section(
        source,
        "bool chatAttachmentFromJSON(",
        "QVector<AIChatAttachment> chatAttachmentsFromJSON(",
    )
    file_ingest = _section(
        source,
        "bool AIAssistant::addAttachmentFiles(",
        "bool AIAssistant::addMimeAttachments(",
    )
    composer_gate = _section(
        source,
        "bool AIAssistant::addComposerAttachment(",
        "void AIAssistant::chooseAttachmentFiles()",
    )
    transcript_decode = _section(
        view,
        "bool conversationImageBytes(",
        "bool conversationAttachmentIsImage(",
    )

    assert 'object[lit("context")] = attachment.context' in serializer
    assert 'object[lit("modifiedUnixMs")]' in serializer
    assert 'object[lit("trust")] = lit("untrusted")' in serializer
    assert 'object.value(lit("context"))' in loader
    assert 'object.value(lit("modifiedUnixMs"))' in loader
    assert "validAttachmentContext(value.context)" in loader

    # Large regular files stay references instead of being base64-expanded. A reference must be
    # canonical and revalidated while it is still in the active composer.
    assert "if(selectedSize > MaxBinaryAttachmentBytes)" in file_ingest
    assert "fileInfoIsSafeReferenceTarget(info, &canonicalPath)" in file_ingest
    assert 'attachment.kind = lit("file-ref")' in file_ingest
    assert 'attachment.encoding = lit("path")' in file_ingest
    assert "attachment.content = canonicalPath" in file_ingest
    assert "attachment.modifiedUnixMs" in file_ingest
    assert "pathReferenceMatchesCurrentFile(attachment)" in composer_gate

    # Loading bounded history validates only the wire metadata. Transcript rendering must never
    # turn a persisted file-ref into fresh filesystem authority or silently read a changed file.
    assert "validPathReference(value)" in loader
    assert "pathReferenceMatchesCurrentFile(value)" not in loader
    assert 'attachment.kind != QLatin1String("image")' in transcript_decode
    assert 'attachment.kind != QLatin1String("file")' in transcript_decode
    assert 'QLatin1String("file-ref")' not in transcript_decode
    assert "QFile" not in transcript_decode
    assert "QFileInfo" not in transcript_decode


def test_send_revalidates_every_file_reference_before_queueing_or_clearing_the_composer() -> None:
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    send_handler = _section(
        source,
        "connect(m_Send, &QPushButton::clicked",
        "promptEdit->send",
    )

    # A file-ref accepted earlier is not durable filesystem authority. Immediately before the
    # request is queued, every composer reference must still identify the same safe canonical file
    # with its recorded size and mtime. Failure reports an error and returns before startChat.
    assert "for(const AIChatAttachment &attachment : m_ComposerAttachments)" in send_handler
    assert "isPathReferenceAttachment(attachment)" in send_handler
    assert "!pathReferenceMatchesCurrentFile(attachment)" in send_handler
    assert "A referenced file changed after it was added. Remove it and add it again." in send_handler
    assert send_handler.index("pathReferenceMatchesCurrentFile") < send_handler.index("startChat(")
    changed_file_branch = send_handler[
        send_handler.index("!pathReferenceMatchesCurrentFile") : send_handler.index(
            "const int queueSize"
        )
    ]
    assert "return;" in changed_file_branch

    # The prompt and attachment snapshot are cleared only after startChat actually grows the queue;
    # a rejected send therefore leaves the user's input and local preview state intact.
    assert "const QVector<AIChatAttachment> attachments = m_ComposerAttachments" in send_handler
    assert "if(m_ChatQueue.size() > queueSize)" in send_handler
    accepted_branch = send_handler[send_handler.index("if(m_ChatQueue.size() > queueSize)") :]
    assert "m_Prompt->clear()" in accepted_branch
    assert "m_ComposerAttachments.clear()" in accepted_branch
    assert "m_ComposerSystemThumbnails.clear()" in accepted_branch
    assert "m_ComposerSystemAnimations.clear()" in accepted_branch


def test_transcript_thumbnails_decode_only_canonical_inline_rasters() -> None:
    view = _source("qrenderdoc/Windows/AI/AIConversationView.cpp")
    byte_decode = _section(
        view,
        "bool conversationImageBytes(",
        "bool conversationAttachmentIsImage(",
    )
    classifier = _section(
        view,
        "bool conversationAttachmentIsImage(",
        "QImage conversationImageThumbnail(",
    )
    format_mapping = _section(
        view,
        "QString conversationRasterMediaTypeForFormat(",
        "bool conversationDecodedRasterImageIsSafe(",
    )
    thumbnail = _section(
        view,
        "QImage conversationImageThumbnail(",
        "QString renderMessageAttachments(",
    )
    transcript_helpers = _section(
        view,
        "const qint64 ConversationImageMaxBytes",
        "QString renderMessageAttachments(",
    )

    # Transcript rendering is allowed to decode only immutable payloads already embedded in the
    # conversation. It never reopens a persisted file reference.
    assert 'attachment.kind != QLatin1String("image")' in byte_decode
    assert 'attachment.kind != QLatin1String("file")' in byte_decode
    assert 'attachment.encoding != QLatin1String("base64")' in byte_decode
    assert "QByteArray::fromBase64(encoded)" in byte_decode
    assert "decoded.toBase64() != encoded" in byte_decode
    assert "ConversationImageMaxBytes" in byte_decode
    assert 'attachment.kind == QLatin1String("file")' in classifier
    assert 'QLatin1String("file-ref")' not in transcript_helpers
    assert "QFile" not in transcript_helpers
    assert "QFileInfo" not in transcript_helpers

    # Installed Qt raster codecs determine the primary path. Document/vector handlers are rejected
    # from the actual detected format before reader.size() is queried, and the declared MIME must
    # match the canonical Qt MIME mapping before that decoder is used. The separate kind=file WIC
    # fallback is covered below and independently rejects the listed known document/vector forms.
    assert "QImageReader::supportedImageFormats()" in format_mapping
    assert "supportedFormats.contains(normalized)" in format_mapping
    for unsafe_format in ("svg", "svgz", "pdf", "eps", "ps"):
        assert f'normalized == "{unsafe_format}"' in format_mapping
    assert 'lit("image/x-") + QString::fromLatin1(normalized)' in format_mapping
    assert "reader.setDecideFormatFromContent(true)" in thumbnail
    assert "reader.setAutoTransform(true)" in thumbnail
    assert "conversationRasterMediaTypeForFormat(reader.format())" in thumbnail
    assert "!detectedMediaType.isEmpty() && detectedMediaType == mediaType" in thumbnail
    assert thumbnail.index("conversationRasterMediaTypeForFormat(reader.format())") < thumbnail.index(
        "reader.size()"
    )

    # Header and post-decode checks independently enforce resource ceilings. A single Qt read keeps
    # animated transcript thumbnails on their first frame; kind=file can then fall back to bounded
    # WIC decoding of the already embedded bytes without granting any path authority.
    assert "ConversationImageMaxDimension" in thumbnail
    assert "ConversationImageMaxPixels" in thumbnail
    assert "conversationDecodedRasterImageIsSafe(image)" in thumbnail
    assert thumbnail.count("reader.read()") == 1
    assert 'attachment.kind == QLatin1String("file")' in thumbnail
    assert "AIFileThumbnail::decodeRasterData" in thumbnail
    assert "ConversationImageMaxBytes" in thumbnail
    assert "QMovie" not in thumbnail


def test_transcript_selection_exposes_a_rounded_add_to_chat_action() -> None:
    header = _source("qrenderdoc/Windows/AI/AIConversationView.h")
    view = _source("qrenderdoc/Windows/AI/AIConversationView.cpp")
    assistant = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    constructor = _section(
        view,
        "AIConversationView::AIConversationView(",
        "void AIConversationView::setMessages(",
    )
    selection = _section(
        view,
        "void AIConversationView::setSelectionAttachmentHandler(",
        "void AIConversationView::changeEvent(",
    )

    assert "setSelectionAttachmentHandler" in header
    assert "QToolButton *m_SelectionAction" in header
    assert "m_SelectionAction = new QToolButton" in constructor
    assert 'translate("AIConversationView", "Add to chat")' in constructor
    assert "border-radius" in constructor
    assert "createStandardContextMenu" in constructor
    assert "addSelectedTextToAttachment" in constructor
    assert "cursor.hasSelection()" in selection
    assert "m_SelectionAttachmentHandler(text)" in selection
    assert "m_Transcript->setSelectionAttachmentHandler" in assistant
    assert "AddTextAttachment" in assistant


def test_shader_views_forward_exact_selections_and_structured_context_through_the_bridge() -> None:
    interface = _source("qrenderdoc/Code/Interface/QRDInterface.h")
    context_header = _source("qrenderdoc/Code/CaptureContext.h")
    context = _source("qrenderdoc/Code/CaptureContext.cpp")
    assistant = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    shader_viewer = _source("qrenderdoc/Windows/ShaderViewer.cpp")
    workspace = _source("replaylab/renderdoc_mcp_bridge/project_shader_workspace.py")

    native_context = _section(
        shader_viewer,
        "ShaderViewer::TextAttachmentSelection ShaderViewer::captureTextAttachmentSelection(",
        "void ShaderViewer::updateTextAttachmentSelection(",
    )
    native_forwarder = _section(
        shader_viewer,
        "void ShaderViewer::addTextAttachmentToChat(",
        "void ShaderViewer::readonly_keyPressed(",
    )
    context_bridge = _section(
        context,
        "void CaptureContext::AddTextToAIAssistant(",
        "AIAssistant *CaptureContext::GetAIAssistant()",
    )
    workspace_forwarder = _section(
        workspace,
        "        def _add_editor_selection_to_chat(",
        "        def _stop_save_worker(",
    )
    assistant_bridge = _section(
        assistant,
        "bool AIAssistant::AddTextAttachment(",
        "bool AIAssistant::addComposerAttachment(",
    )

    # The optional fourth parameter preserves source compatibility for existing three-argument
    # callers. Structured ShaderViewer and Shader Workspace callers use the same bridge with v1
    # context as the fourth argument.
    assert "virtual void AddTextToAIAssistant" in interface
    assert "const rdcstr &contextJSON = {}" in interface
    assert "const rdcstr &contextJSON = {}" in context_header
    assert "m_Ctx.AddTextToAIAssistant(" in native_forwarder
    assert "selection.title, selection.text, selection.source" in native_forwarder
    assert "selection.contextJSON" in native_forwarder
    assert "self.ctx.AddTextToAIAssistant(" in workspace_forwarder
    assert "title, text, source," in workspace_forwarder
    assert "json.dumps(context" in workspace_forwarder

    # This bridge is deliberately code-specific: both ShaderViewer and the built-in Shader
    # Workspace arrive here, and the bool selects the typed `code` attachment kind while context
    # remains separately parsed and validated.
    assert "GetAIAssistant()->AddTextAttachment(title, text, source, true, contextJSON)" in context_bridge
    assert "ShowAIAssistant();" in context_bridge
    assert "QJsonDocument::fromJson" in assistant_bridge
    assert "MaxAttachmentContextBytes" in assistant_bridge
    assert "validAttachmentContext(contextDocument.object())" in assistant_bridge

    # Context carries opaque lookup identities and capture/shader relations, never an absolute
    # source path. The Host can re-resolve these hints against the capture before using them.
    for field in (
        'lit("replaylab-attachment-context/v1")',
        'lit("origin")',
        'lit("capture")',
        'lit("shader")',
        'lit("relations")',
        'lit("bound-at-event")',
    ):
        assert field in native_context
    assert "QFileInfo(sourceName).fileName()" in native_context
    assert "QCryptographicHash::Sha256" in native_context
    assert "CurEvent()" in native_context
    assert "GetShader(m_Stage) == shaderId" in native_context
    assert "QJsonDocument(context).toJson(QJsonDocument::Compact)" in native_context
    assert "os.path.basename(source_path)" in workspace_forwarder
    workspace_context = _section(workspace, "def _selection_attachment(", "def _normal_path(")
    assert "_selection_attachment(" in workspace_forwarder
    assert '"schema": "replaylab-attachment-context/v1"' in workspace_context
    assert '"panel": "shader-workspace"' in workspace_context


def test_active_multi_step_task_uses_a_rounded_hover_popup_with_wrapped_status_rows() -> None:
    header = _source("qrenderdoc/Windows/AI/AIAssistant.h")
    source = _source("qrenderdoc/Windows/AI/AIAssistant.cpp")
    hover_button = _section(
        source,
        "class AITaskProgressButton",
        "AIAssistant::AIAssistant(",
    )
    progress_label = _section(
        source,
        "class AITaskProgressLabel",
        "class AITaskProgressRow",
    )
    progress_row = _section(
        source,
        "class AITaskProgressRow",
        "class AITaskProgressButton",
    )
    constructor = _section(
        source,
        "AIAssistant::AIAssistant(",
        "AIAssistant::~AIAssistant()",
    )
    projection = _section(
        source,
        "void AIAssistant::updateTaskProgressUI()",
        "void AIAssistant::updateComposerState()",
    )
    menu_rows = _section(
        projection,
        "const QString stepText",
        "m_TaskProgress->show();",
    )

    assert "QToolButton *m_TaskProgress" in header
    assert "QMenu *m_TaskProgressMenu" in header
    assert "void enterEvent(QEvent *event) override" in hover_button
    assert "focusInEvent" not in hover_button
    assert "menu()->popup(popupPosition)" in hover_button
    assert "QApplication::applicationState() != Qt::ApplicationActive" in hover_button
    assert "!topLevel->isActiveWindow()" in hover_button

    # Widget actions give the bounded popup real multi-line rows. Each state is also encoded as a
    # distinct circle, while running work animates only while the popup is visible.
    assert "setTextFormat(Qt::PlainText)" in progress_label
    assert "setWordWrap(true)" in progress_label
    assert "QTextOption::WrapAtWordBoundaryOrAnywhere" in progress_label
    assert "bool hasHeightForWidth() const override" in progress_label
    assert "m_Label->heightForWidth(textWidth)" in progress_row
    assert "State::Completed" in progress_row
    assert "State::Failed" in progress_row
    assert "State::Waiting" in progress_row
    assert "painter.drawEllipse(circle)" in progress_row
    assert "QDateTime::currentMSecsSinceEpoch() / 90" in progress_row
    assert "painter.drawArc(circle, spinnerAngle" in progress_row
    assert "m_SpinnerAngle" not in progress_row
    assert "diagnosticColor(this, DiagnosticLevel::Success)" in progress_row
    assert "diagnosticColor(this, DiagnosticLevel::Error)" in progress_row
    assert "m_SpinnerTimer->start()" in progress_row
    assert "m_SpinnerTimer->stop()" in progress_row
    assert "void updateProgress(const QString &text, State state, int rowWidth)" in progress_row
    assert "ensureSpinnerTimer()" in progress_row

    assert "m_TaskProgress = new AITaskProgressButton(composer)" in constructor
    assert "m_TaskProgressMenu = new QMenu(m_TaskProgress)" in constructor
    assert "&QGuiApplication::applicationStateChanged" in constructor
    assert "m_TaskProgressMenu->close()" in constructor
    assert "border-radius: 12px" in constructor
    assert "border-radius: 8px" in constructor
    # The progress pill belongs immediately above the input rather than in the transcript.
    assert constructor.index("composerLayout->addWidget(m_TaskProgress") < constructor.index(
        "AIPromptEdit *promptEdit"
    )

    # Only the selected conversation's running main task may project child work, and one-step tasks
    # do not create visual noise. Tool/provider rows cannot create task entries on their own.
    assert "conversationExecution(thread->id)" in projection
    assert "!m_RunningTasks.contains(taskId)" in projection
    assert "entries.size() < 2" in projection
    assert 'activity.kind == lit("child-") + planTaskId' in projection
    assert "if(!childLifecycle && !(waitingLifecycle" in projection
    assert "if(entry.startedMs <= 0" in projection
    assert 'entry.status != lit("completed")' in projection

    assert 'tr("Step %1 of %2")' in menu_rows
    assert 'tr("waiting")' in menu_rows
    assert "entry.title.simplified()" in menu_rows
    assert "title.size() > 160" in menu_rows
    assert "qBound(320, progressParentWidth - 32, 680)" in menu_rows
    assert "bool reuseRows" in menu_rows
    assert "dynamic_cast<AITaskProgressRow *>" in menu_rows
    assert "row->updateProgress(" in menu_rows
    assert "QWidgetAction *rowAction = new QWidgetAction" in menu_rows
    assert "rowAction->setData(progressRow.id)" in menu_rows
    assert "new AITaskProgressRow(" in menu_rows
    assert "m_TaskProgressMenu->addAction(rowAction)" in menu_rows
    # Menu rows are updated from the bounded task projection and rebuilt only when its
    # identity/order changes. Raw event/provider/tool objects, arguments, results, and activity
    # detail are never rendered by this code path.
    for raw_payload_token in (
        "eventData",
        "providerOutput",
        "toolArguments",
        "toolResult",
        "activity.detail",
    ):
        assert raw_payload_token not in menu_rows

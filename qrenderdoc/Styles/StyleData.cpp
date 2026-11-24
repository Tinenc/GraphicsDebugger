/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2017-2026 Baldur Karlsson
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

#include "StyleData.h"
#include <QApplication>
#include "Code/QRDUtils.h"
#include "RDStyle/RDStyle.h"
#include "RDTweakedNativeStyle/RDTweakedNativeStyle.h"

namespace StyleData
{
TranslatableString::operator QString() const
{
  return QApplication::translate("RDStyle", sourceText);
}

const ThemeDescriptor availStyles[] = {
    ThemeDescriptor(
        lit("RDLight"), QT_TRANSLATE_NOOP("RDStyle", "Light"),
        QT_TRANSLATE_NOOP(
            "RDStyle", "Light: Cross-platform custom RenderDoc dark theme (black-on-white)."),
        []() { return new RDStyle(RDStyle::Light); }),

    ThemeDescriptor(
        lit("RDDark"), QT_TRANSLATE_NOOP("RDStyle", "Dark"),
        QT_TRANSLATE_NOOP(
            "RDStyle", "Dark: Cross-platform custom RenderDoc dark theme (white-on-black)."),
        []() { return new RDStyle(RDStyle::Dark); }),

    ThemeDescriptor(
        lit("ultra"), QT_TRANSLATE_NOOP("RDStyle", "Black"),    // i18n-ignore: retain the persisted theme ID for existing UI.config files
        QT_TRANSLATE_NOOP(
            "RDStyle",
            "Black: Unity-inspired dark theme with neutral greys and blue highlights."),
        []() { return new RDStyle(RDStyle::Ultra); }),

    ThemeDescriptor(
        lit("Native"), QT_TRANSLATE_NOOP("RDStyle", "Native"),
        QT_TRANSLATE_NOOP("RDStyle",
                          "Native: uses the built-in Qt native widgets for your platform."),
        []() { return new RDTweakedNativeStyle(NULL); }),
};

const int numAvailable = sizeof(availStyles) / sizeof(ThemeDescriptor);
};

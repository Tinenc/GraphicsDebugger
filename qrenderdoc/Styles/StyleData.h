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

#include <QString>
#include <functional>

class QStyle;

namespace StyleData
{
// Stores only the source text during static initialisation, then translates on each UI access. This
// avoids freezing theme labels before the application's translator has been installed and also
// makes a later language refresh observe the current translator.
struct TranslatableString
{
  explicit TranslatableString(const char *source) : sourceText(source) {}

  operator QString() const;

private:
  const char *sourceText;
};

struct ThemeDescriptor
{
  ThemeDescriptor(const QString &id, const char *name, const char *desc,
                  std::function<QStyle *()> cr)
      : styleID(id), styleName(name), styleDescription(desc), creator(cr)
  {
  }

  QString styleID;
  TranslatableString styleName;
  TranslatableString styleDescription;
  std::function<QStyle *()> creator;
};

extern const ThemeDescriptor availStyles[];
extern const int numAvailable;
};

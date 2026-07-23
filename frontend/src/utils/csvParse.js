/**
 * Parse one CSV/TSV line with RFC 4180-style double quotes and optional single-quoted fields.
 *
 * - Commas inside a value: wrap in double quotes → "hello, world"
 * - Double quotes inside a value: double them → "Say ""hello"""
 * - Single-quoted whole field (convenience): 'O''Brien, Jr.'
 * - Unquoted values: commas split fields; single quotes are kept as-is → O'Brien
 */
export function parseCsvLine(line, delimiter = ',') {
  const fields = []
  let i = 0
  const len = line.length

  while (true) {
    let field = ''

    if (i >= len) {
      fields.push(field)
      break
    }

    const ch = line[i]

    if (ch === '"') {
      i++
      while (i < len) {
        if (line[i] === '"') {
          if (i + 1 < len && line[i + 1] === '"') {
            field += '"'
            i += 2
          } else {
            i++
            break
          }
        } else {
          field += line[i]
          i++
        }
      }
    } else if (ch === "'") {
      i++
      while (i < len) {
        if (line[i] === "'") {
          if (i + 1 < len && line[i + 1] === "'") {
            field += "'"
            i += 2
          } else {
            i++
            break
          }
        } else {
          field += line[i]
          i++
        }
      }
    } else {
      while (i < len && line[i] !== delimiter) {
        field += line[i]
        i++
      }
      field = field.trim()
    }

    fields.push(field)

    if (i >= len) break
    if (line[i] === delimiter) i++
  }

  return fields
}

/** Pick comma vs tab for pasted grid rows (Excel copies as TSV). */
export function detectPasteDelimiter(lines, columnCount) {
  const sample = lines.find(line => line.trim())
  if (!sample) return ','

  const tabCount = (sample.match(/\t/g) || []).length
  if (tabCount === 0) return ','

  const tabFields = parseCsvLine(sample, '\t')
  const commaFields = parseCsvLine(sample, ',')

  if (tabFields.length >= columnCount && tabFields.length >= commaFields.length) {
    return '\t'
  }
  return ','
}

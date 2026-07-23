import * as XLSX from 'xlsx'
import { parseCsvLine } from './csvParse'

export function detectUploadFormat(fileName = '') {
  const ext = String(fileName).toLowerCase().split('.').pop()
  if (ext === 'xlsx') return 'xlsx'
  if (ext === 'xls') return 'xls'
  if (ext === 'txt' || ext === 'tsv') return 'txt'
  return 'csv'
}

export function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

export function parseCsvPreview(csvText, delimiter = ',', hasHeader = true, maxRows = 5) {
  const lines = String(csvText || '').trim().split(/\r?\n/).filter(Boolean)
  if (!lines.length) return { headers: [], rows: [], rowCount: 0 }
  const sep = delimiter === '\\t' ? '\t' : delimiter
  const headers = hasHeader
    ? parseCsvLine(lines[0], sep)
    : parseCsvLine(lines[0], sep).map((_, i) => `col_${i + 1}`)
  const start = hasHeader ? 1 : 0
  const rowCount = Math.max(0, lines.length - start)
  const rows = lines.slice(start, start + maxRows).map(line => parseCsvLine(line, sep))
  return { headers, rows, rowCount }
}

export function parseExcelPreview(arrayBuffer, hasHeader = true, maxRows = 5) {
  const wb = XLSX.read(arrayBuffer, { type: 'array', cellDates: false })
  const sheetName = wb.SheetNames[0]
  if (!sheetName) return { headers: [], rows: [], rowCount: 0 }

  const sheet = wb.Sheets[sheetName]
  const allRows = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '', raw: false })
    .map(row => (Array.isArray(row) ? row : Object.values(row)).map(cell => String(cell ?? '').trim()))

  if (!allRows.length) return { headers: [], rows: [], rowCount: 0 }

  const width = Math.max(...allRows.map(row => row.length), 1)
  const headers = hasHeader
    ? allRows[0].map((h, i) => String(h || '').trim() || `col_${i + 1}`)
    : Array.from({ length: width }, (_, i) => `col_${i + 1}`)
  const dataStart = hasHeader ? 1 : 0
  const rowCount = Math.max(0, allRows.length - dataStart)
  const rows = allRows.slice(dataStart, dataStart + maxRows).map(row => {
    const padded = [...row]
    while (padded.length < width) padded.push('')
    return padded.slice(0, width)
  })

  return { headers, rows, rowCount }
}

export async function readUploadFile(file) {
  const format = detectUploadFormat(file.name)
  if (format === 'xls') {
    throw new Error('Legacy .xls is not supported. Save the workbook as .xlsx and retry.')
  }

  if (format === 'xlsx') {
    const buffer = await file.arrayBuffer()
    const preview = parseExcelPreview(buffer, true)
    return {
      name: file.name,
      format: 'xlsx',
      csvText: '',
      base64: arrayBufferToBase64(buffer),
      preview,
      rowCount: preview.rowCount,
    }
  }

  const csvText = await file.text()
  const preview = parseCsvPreview(csvText, ',', true)
  return {
    name: file.name,
    format: format === 'txt' ? 'txt' : 'csv',
    csvText,
    base64: '',
    preview,
    rowCount: preview.rowCount,
  }
}

export function uploadPreview(uploadFile, delimiter, hasHeader) {
  if (!uploadFile) return { headers: [], rows: [] }
  if (uploadFile.format === 'xlsx' && uploadFile.preview) {
    return {
      headers: uploadFile.preview.headers,
      rows: uploadFile.preview.rows,
    }
  }
  if (uploadFile.csvText) {
    const parsed = parseCsvPreview(uploadFile.csvText, delimiter, hasHeader)
    return { headers: parsed.headers, rows: parsed.rows }
  }
  return { headers: [], rows: [] }
}

export function uploadDataRowCount(uploadFile, delimiter, hasHeader) {
  if (!uploadFile) return 0
  if (uploadFile.rowCount != null) return uploadFile.rowCount
  if (uploadFile.csvText) {
    return parseCsvPreview(uploadFile.csvText, delimiter, hasHeader).rowCount
  }
  return 0
}

/**
 * Google Apps Script for Real-Time Stock Count Auto-Fill
 * =======================================================
 * HOW TO SETUP (Takes 1 minute):
 * 1. Open your Google Sheet.
 * 2. Click on "Extensions" -> "Apps Script".
 * 3. Delete any default code and PASTE THIS ENTIRE SCRIPT.
 * 4. Click "Deploy" (top right) -> "New deployment".
 * 5. Select type: "Web app".
 * 6. Under "Who has access", select: "Anyone". (CRITICAL)
 * 7. Click "Deploy", authorize permissions if prompted, and COPY the "Web App URL".
 * 8. Paste the URL into your bot's .env file:
 *    GOOGLE_SHEET_WEBHOOK_URL="https://script.google.com/macros/s/xxxxxx/exec"
 */

function doPost(e) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    
    // Ensure header row exists on first use
    if (sheet.getLastRow() === 0) {
      setupHeaders(sheet);
    }
    
    // Parse incoming JSON payload from Telegram Bot
    var data = JSON.parse(e.postData.contents);
    
    var timestamp = data.timestamp || Utilities.formatDate(new Date(), "Asia/Bangkok", "yyyy-MM-dd HH:mm:ss");
    var crew = data.crew || "Unknown";
    var shelf = (data.shelf || "").toUpperCase();
    var barcode = "'" + String(data.barcode || ""); // Prefix with ' to guarantee text format
    var itemName = data.name || "-";
    var qty = Number(data.qty) || 1;
    
    // Append the row
    sheet.appendRow([
      timestamp,
      crew,
      shelf,
      barcode,
      itemName,
      qty
    ]);
    
    // Format the barcode cell explicitly as text
    var lastRow = sheet.getLastRow();
    sheet.getRange(lastRow, 4).setNumberFormat("@");
    sheet.getRange(lastRow, 6).setNumberFormat("#,##0");
    
    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      message: "Row appended successfully",
      row: lastRow
    })).setMimeType(ContentService.MimeType.JSON);
    
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}

function setupHeaders(sheet) {
  var headers = [
    "Date & Time",
    "Crew Member",
    "Shelf Location",
    "Barcode Number",
    "Item Name / Description",
    "Quantity"
  ];
  
  sheet.appendRow(headers);
  var headerRange = sheet.getRange(1, 1, 1, headers.length);
  headerRange.setFontWeight("bold");
  headerRange.setBackground("#1E3A8A");
  headerRange.setFontColor("#FFFFFF");
  headerRange.setHorizontalAlignment("center");
  sheet.setFrozenRows(1);
}

function doGet(e) {
  return ContentService.createTextOutput("Stock Count Webhook is online and active!").setMimeType(ContentService.MimeType.TEXT);
}

function doPost(e) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    
    // Auto-create headers on row 1 if sheet is empty
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(["Date & Time", "Crew Member", "Shelf Location", "Barcode Number", "Item Name", "Quantity", "Front Photo", "Barcode Photo"]);
      var headerRange = sheet.getRange(1, 1, 1, 8);
      headerRange.setFontWeight("bold");
      headerRange.setBackground("#1E3A8A");
      headerRange.setFontColor("#FFFFFF");
      headerRange.setHorizontalAlignment("center");
      sheet.setFrozenRows(1);
      sheet.setColumnWidth(7, 120);
      sheet.setColumnWidth(8, 120);
    }
    
    var data = JSON.parse(e.postData.contents);
    var timestamp = data.timestamp || Utilities.formatDate(new Date(), "Asia/Bangkok", "yyyy-MM-dd HH:mm:ss");
    var crew = data.crew || "Unknown";
    var shelf = String(data.shelf || "").toUpperCase();
    var barcode = "'" + String(data.barcode || ""); // Prefix with ' to keep barcode as text
    var itemName = data.name || "-";
    var qty = Number(data.qty) || 1;
    var frontUrl = data.photo_front_url || data.photo_url || "";
    var barcodeUrl = data.photo_barcode_url || "";
    
    // Clickable HD Image Formula: Click image to open Full HD original in new tab!
    var frontFormula = frontUrl ? '=HYPERLINK("' + frontUrl + '", IMAGE("' + frontUrl + '", 1))' : "";
    var barcodeFormula = barcodeUrl ? '=HYPERLINK("' + barcodeUrl + '", IMAGE("' + barcodeUrl + '", 1))' : "";
    
    sheet.appendRow([timestamp, crew, shelf, barcode, itemName, qty, frontFormula, barcodeFormula]);
    
    var lastRow = sheet.getLastRow();
    sheet.getRange(lastRow, 4).setNumberFormat("@"); // Format barcode as text
    sheet.getRange(lastRow, 6).setNumberFormat("#,##0"); // Format quantity
    sheet.getRange(lastRow, 1, 1, 8).setVerticalAlignment("middle");
    sheet.setRowHeight(lastRow, 85); // High clear row height for sharp HD images
    
    return ContentService.createTextOutput("OK");
  } catch (err) {
    return ContentService.createTextOutput("Error: " + err.toString());
  }
}

function doPost(e) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    
    // Auto-create headers on row 1 if sheet is empty
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(["Date & Time", "Crew Member", "Shelf Location", "Barcode Number", "Item Name", "Quantity", "Product Photo"]);
      var headerRange = sheet.getRange(1, 1, 1, 7);
      headerRange.setFontWeight("bold");
      headerRange.setBackground("#1E3A8A");
      headerRange.setFontColor("#FFFFFF");
      headerRange.setHorizontalAlignment("center");
      sheet.setFrozenRows(1);
      sheet.setColumnWidth(7, 100);
    }
    
    var data = JSON.parse(e.postData.contents);
    var timestamp = data.timestamp || Utilities.formatDate(new Date(), "Asia/Bangkok", "yyyy-MM-dd HH:mm:ss");
    var crew = data.crew || "Unknown";
    var shelf = String(data.shelf || "").toUpperCase();
    var barcode = "'" + String(data.barcode || ""); // Prefix with ' to keep barcode as text
    var itemName = data.name || "-";
    var qty = Number(data.qty) || 1;
    var photoUrl = data.photo_url || "";
    
    var imageFormula = photoUrl ? '=IMAGE("' + photoUrl + '", 1)' : "";
    sheet.appendRow([timestamp, crew, shelf, barcode, itemName, qty, imageFormula]);
    
    var lastRow = sheet.getLastRow();
    sheet.getRange(lastRow, 4).setNumberFormat("@"); // Format barcode as text
    sheet.getRange(lastRow, 6).setNumberFormat("#,##0"); // Format quantity
    sheet.getRange(lastRow, 1, 1, 7).setVerticalAlignment("middle");
    if (photoUrl) {
      sheet.setRowHeight(lastRow, 65); // Give height so image is clear
    }
    
    return ContentService.createTextOutput("OK");
  } catch (err) {
    return ContentService.createTextOutput("Error: " + err.toString());
  }
}

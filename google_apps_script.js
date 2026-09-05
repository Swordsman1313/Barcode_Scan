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
    
    // Save photos permanently to Google Drive so they NEVER expire
    var frontDrive = saveImageToDrive(frontUrl, "front_" + (data.barcode || "item") + "_" + new Date().getTime() + ".jpg");
    var barcodeDrive = saveImageToDrive(barcodeUrl, "barcode_" + (data.barcode || "item") + "_" + new Date().getTime() + ".jpg");
    
    // Clickable HD Image Formula: Shows thumbnail in cell and opens HD image on click
    var frontFormula = frontDrive.viewUrl ? '=HYPERLINK("' + frontDrive.viewUrl + '", IMAGE("' + frontDrive.thumbUrl + '", 1))' : "";
    var barcodeFormula = barcodeDrive.viewUrl ? '=HYPERLINK("' + barcodeDrive.viewUrl + '", IMAGE("' + barcodeDrive.thumbUrl + '", 1))' : "";
    
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

/**
 * Downloads image from temporary URL and saves permanently in Google Drive folder "Stock_Count_Photos"
 */
function saveImageToDrive(url, filename) {
  if (!url || !url.startsWith("http")) {
    return { thumbUrl: "", viewUrl: "" };
  }
  try {
    var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (response.getResponseCode() === 200) {
      var blob = response.getBlob().setName(filename);
      var folderName = "Stock_Count_Photos";
      var folders = DriveApp.getFoldersByName(folderName);
      var folder = folders.hasNext() ? folders.next() : DriveApp.createFolder(folderName);
      folder.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      
      var file = folder.createFile(blob);
      file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      
      var fileId = file.getId();
      var thumbUrl = "https://drive.google.com/thumbnail?id=" + fileId + "&sz=w1000";
      var viewUrl = "https://drive.google.com/uc?export=view&id=" + fileId;
      return { thumbUrl: thumbUrl, viewUrl: viewUrl };
    }
  } catch (e) {
    Logger.log("Drive save error: " + e);
  }
  // Fallback to original URL if Drive save fails
  return { thumbUrl: url, viewUrl: url };
}

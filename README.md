# Receipts Tool

Double-click `Payment Receipt Generator Tool.exe` to open the receipt generator.

The app runs locally on this computer. Nothing is uploaded anywhere.

The app checks GitHub for updates each time it opens. If a newer version is available, it shows a notification and marks `Settings`, where you can use `Update From GitHub`.

Generated receipt PDFs are protected from normal PDF editing.

Use `Settings` to save business defaults and upload a custom receipt logo. The app keeps `assets/logo.png` beside the executable so updates preserve the current logo.

When you use the app, it saves its working files beside the executable:

- `data/receipts.sqlite`
- `receipts/{year}/...pdf`
- `backups/receipts-{timestamp}.sqlite`

Developer files are inside the `source` folder.

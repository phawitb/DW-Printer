from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import os
import subprocess
from datetime import datetime

app = FastAPI()

UPLOAD_DIR = "pdfs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

PRINTER_NAME = "HP_DeskJet"  # Change this to your printer name from `lpstat -p -d`

@app.post("/upload-pdf")
async def upload_pdf(uid: str = Form(...), file: UploadFile = File(...)):
    """
    Receive a PDF file + user ID, save it, and send it to the printer.
    """
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "Only PDF files allowed"})
    
    # Create user-specific folder
    user_dir = os.path.join(UPLOAD_DIR, uid)
    os.makedirs(user_dir, exist_ok=True)

    # Save file with timestamp to avoid overwrite
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_name = f"{timestamp}_{file.filename}"
    save_path = os.path.join(user_dir, save_name)

    with open(save_path, "wb") as f:
        f.write(await file.read())
        
    return {
            "status": "uploaded",
            "uid": uid,
            "filename": save_name,
            "message": f"Printed {save_name} for user {uid}"
        }

#    try:
#        # Print using lp command
#        subprocess.run(["lp", "-d", PRINTER_NAME, save_path], check=True)
#
#        return {
#            "status": "success",
#            "uid": uid,
#            "filename": save_name,
#            "message": f"Printed {save_name} for user {uid}"
#        }
#    except Exception as e:
#        return {"status": "uploadfail"}

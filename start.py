import os, sys, traceback

os.chdir(r"C:\Users\ThanarojThanarojthan\shipify-rag")
sys.path.insert(0, r"C:\Users\ThanarojThanarojthan\shipify-rag")

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
        import uvicorn
        print("Starting Shipify Admin on http://localhost:8001")
        uvicorn.run("admin.routes:app", host="0.0.0.0", port=8001, reload=True)
    except Exception as e:
        traceback.print_exc()
        with open("startup_error.txt", "w") as f:
            f.write(traceback.format_exc())
        print("ERROR saved to startup_error.txt")
        input("Press Enter to close...")

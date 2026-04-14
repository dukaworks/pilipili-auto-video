from api import create_app
# ============================================================
# 启动入口
# ============================================================

app = create_app()

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True, log_level="info")

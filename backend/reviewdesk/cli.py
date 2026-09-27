"""命令行：reviewdesk serve | worker | init | create-user"""
from __future__ import annotations

import argparse
import logging


def main() -> None:
    ap = argparse.ArgumentParser(prog="reviewdesk", description="综述协作平台")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="启动 Web 服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--workers", type=int, default=1)
    sub.add_parser("worker", help="启动后台任务 worker（服务器 / 云端版）")
    sub.add_parser("init", help="建表并创建默认管理员")
    cu = sub.add_parser("create-user", help="创建用户")
    cu.add_argument("email")
    cu.add_argument("name")
    cu.add_argument("password")
    cu.add_argument("--role", default="student", choices=["student", "mentor", "admin"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("reviewdesk.main:app", host=args.host, port=args.port, workers=args.workers)
    elif args.cmd == "worker":
        from .jobs import worker_loop
        from .main import bootstrap

        bootstrap()
        worker_loop()
    elif args.cmd == "init":
        from .main import bootstrap

        bootstrap()
        print("数据库已初始化")
    elif args.cmd == "create-user":
        from sqlalchemy import select

        from .db import SessionLocal
        from .main import bootstrap
        from .models import Tenant, User
        from .security import hash_password

        bootstrap()
        db = SessionLocal()
        t = db.scalar(select(Tenant).limit(1))
        db.add(User(tenant_id=t.id, email=args.email.lower(), name=args.name, password_hash=hash_password(args.password), role=args.role))
        db.commit()
        print(f"已创建用户 {args.email}（{args.role}）")


if __name__ == "__main__":
    main()

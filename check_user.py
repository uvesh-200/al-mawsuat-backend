import asyncio
import asyncpg


async def check():
    conn = await asyncpg.connect("postgresql://mawsuat:mawsuat@localhost:5432/mawsuat")
    rows = await conn.fetch("SELECT id, email, role, is_active, is_superuser FROM users")
    print(f"Users found: {len(rows)}")
    for r in rows:
        print(f"  {r['email']} | role={r['role']} | active={r['is_active']} | superuser={r['is_superuser']}")
    await conn.close()


asyncio.run(check())

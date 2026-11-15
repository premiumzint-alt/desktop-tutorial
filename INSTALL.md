# คู่มือติดตั้งและใช้งาน Sentinel Server Monitor

ไฟล์นี้เป็นคู่มือสำหรับผู้ติดตั้งและผู้ใช้งาน แยกจากส่วนของซอร์สโค้ด
โปรแกรมหลักอยู่ใน `backend/` (API), `collector/` (ตัวเก็บตัวชี้วัด), `frontend/` (หน้า dashboard) และสคริปต์ต่างๆ อยู่ใน `scripts/`

---

## 1. สิ่งที่ต้องเตรียมก่อนติดตั้ง (บนเครื่องเป้าหมาย)

| รายการ | รายละเอียด |
|--------|-------------|
| ระบบปฏิบัติการ | Windows 10 (build 19045 ขึ้นไป) หรือ Windows 11 |
| Python | 3.11 ขึ้นไป และต้องใช้งานจริงบน PATH (คำสั่ง `python` ต้องตอบเวอร์ชัน ไม่ใช่ตัวชวนติดตั้งจาก Microsoft Store) |
| MongoDB | ติดตั้งเป็น Windows service แล้ว รันอยู่ที่ `127.0.0.1:27017` (ชื่อบริการ `MongoDB`) หรือมี Docker Desktop |
| อินเทอร์เน็ต | ต้องโหลด Python package จาก PyPI ได้ (สำหรับสร้าง `.venv` ครั้งแรก) |
| สิทธิ์ | รัน PowerShell แบบ **ผู้ดูแลระบบ** (Run as administrator) เพื่อลงทะเบียนงานอัตโนมัติ |
| พื้นที่ดิสก์ | เหลืออย่างน้อยประมาณ 2 GB (สำหรับ venv, package, ฐานข้อมูล, log) |

ตรวจสอบความพร้อมก่อนติดตั้ง:

```powershell
python --version
Get-Service MongoDB | Select-Object Status, StartType
```

---

## 2. วิธีติดตั้ง (คำสั่งเดียว)

1. คัดลอกโฟลเดอร์โปรเจกต์ทั้งหมดไปยังเครื่องเป้าหมาย (เช่น `C:\Sentinel`)
2. เปิด **PowerShell แบบผู้ดูแลระบบ** ที่โฟลเดอร์โปรเจกต์
3. รันคำสั่ง

```powershell
.\Install.cmd
```

หรือ

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

ตัวติดตั้งจะทำให้อัตโนมัติ:
- สร้างสภาพแวดล้อม Python ที่ `collector\.venv`
- ติดตั้ง Python package ที่จำเป็นทั้ง backend และ collector
- สร้าง `backend\.env` พร้อมรหัสลับสุ่ม 3 ตัว (ถ้ายังไม่มีไฟล์นี้)
- ลงทะเบียนงานอัตโนมัติ 5 งาน (API, Dashboard, Collector, Watchdog, Backup) ให้เริ่มทำงานเมื่อล็อกอินเข้าเครื่อง
- รอให้ API (`127.0.0.1:8000`) และ dashboard (`127.0.0.1:8001`) พร้อมใช้งาน
- ถ้ายังไม่มีบัญชีแอดมิน จะถามตั้งชื่อผู้ใช้และรหัสผ่าน (อย่างน้อย 12 ตัวอักษร)
- เปิดหน้า dashboard ให้โดยอัตโนมัติ

ตัวติดตั้งทำซ้ำได้ (idempotent) รันซ้ำกี่ครั้งก็ไม่เสียของเดิม

---

## 3. ข้อมูลบนเครื่องใหม่ (เริ่มจากศูนย์)

การติดตั้งบนเครื่องใหม่ไม่ได้ย้ายข้อมูลเดิมมาด้วย ทุกอย่างเริ่มใหม่:

| ข้อมูล | ที่อยู่ | ผลกระทบบนเครื่องใหม่ |
|--------|--------|------------------------|
| บัญชีผู้ใช้, เกณฑ์, กฎเตือน, ช่องทางแจ้งเตือน | `backend\monitor.db` (SQLite) | ไม่มีของเดิม ต้องตั้งค่าใหม่ทั้งหมด |
| คีย์ลับของระบบ | `backend\.env` | สร้างใหม่แบบสุ่มใหม่ทุกครั้ง |
| baseline ไฟล์ที่ติดตาม, offset ของ Security log | `collector\state\` | เริ่มใหม่ ระบบจะสร้าง baseline ให้เองในรอบแรก |
| ตัวชี้วัดและเหตุการณ์ | MongoDB (`server_monitor`) | ไม่มีประวัติเดิม เก็บใหม่ตาม retention ที่ตั้งไว้ |
| ข้อมูลสำรอง | `backups\` | ไม่มีของเดิม ต้องคัดลอกมาเองถ้าต้องการ |

ถ้าต้องการย้ายข้อมูลจากเครื่องเก่าไปเครื่องใหม่ ให้คัดลอก `backend\monitor.db`, `backend\.env` และโฟลเดอร์ `collector\state\` ไปวางที่ตำแหน่งเดิมก่อนรันตัวติดตั้ง (ตัวติดตั้งจะไม่เขียนทับ `.env` ที่มีอยู่แล้ว)

---

## 4. การเชื่อมต่อทั้งหมด

### ภายในเครื่อง (localhost เท่านั้น)

| ส่วน | ที่อยู่ | พอร์ต | หมายเหตุ |
|------|--------|-------|-----------|
| API (FastAPI) | `http://127.0.0.1:8000` | 8000 | ผูกกับ loopback เท่านั้น เข้าจากเครือข่ายภายนอกไม่ได้ |
| Dashboard | `http://127.0.0.1:8001/` | 8001 | เปิดผ่านเบราว์เซอร์หรือแอปเดสก์ท็อป |
| เอกสาร API | `http://127.0.0.1:8000/docs` | 8000 | Swagger UI สำหรับทดสอบเรียก API |
| สถานะระบบ | `http://127.0.0.1:8000/health` | 8000 | คืน `{"status":"ok","mongodb":"ok"}` เมื่อทุกอย่างปกติ |
| MongoDB | `mongodb://127.0.0.1:27017` | 27017 | ฐานข้อมูลชื่อ `server_monitor` |

### Collector → API
- ตั้งค่าใน `backend\.env`: `MONITOR_API_URL` (ค่าเริ่มต้น `http://127.0.0.1:8000`) และ `COLLECTOR_API_KEY`
- collector ส่งหัวข้อ `X-Collector-Key` ทุกคำขอ ไปที่ `/api/v1/collector/metrics`, `/security-events`, `/scan-results`
- ถ้า API ยังไม่พร้อม collector จะพักข้อมูลไว้ใน `collector\state\spool.jsonl` แล้วส่งใหม่เมื่อ API กลับมา

### Dashboard → API
- ผู้ใช้ล็อกอินที่หน้า dashboard ระบบจะเรียก `POST /api/v1/auth/login` แล้วเก็บ bearer token ไว้ใน `sessionStorage` (เบราว์เซอร์) หรือ `localStorage` (โหมดเดสก์ท็อป)
- ทุกคำขอส่งหัวข้อ `Authorization: Bearer <token>`
- token หมดอายุตาม `ACCESS_TOKEN_MINUTES` (เริ่มต้น 30 นาที) แล้วหน้าเว็บจะพากลับไปหน้าล็อกอิน
- CORS อนุญาตเฉพาะ `http://localhost:8001` และ `http://127.0.0.1:8001` (ตั้งที่ `CORS_ORIGINS` ใน `.env`)

### การแจ้งเตือนออกภายนอก
ตั้งค่าในหน้า Settings → Alert notifications รองรับ Slack, Discord, Telegram และ webhook ทั่วไป (ส่ง payload เต็มของ alert) ตัวเลขโทรศัพท์ถูกเก็บฝั่งเซิร์ฟเวอร์ และตอบกลับมาแบบปิดบังบางส่วนเท่านั้น

### การดูข้อมูลของเครื่องอื่น
ตัว collector ส่ง `host` ไปกับทุก payload (ค่าเริ่มต้นคือชื่อเครื่อง ตั้ง `MONITOR_HOST_ID` เพื่อกำหนดเองได้) ดังนั้นหน้า dashboard ของเครื่องกลางจะเห็นข้อมูลของทุกเครื่องที่ตั้งค่าให้ส่งเข้ามา โดยแต่ละเครื่องต้องรัน collector ของตัวเองและชี้ `MONITOR_API_URL` ไปที่เครื่องกลาง

---

## 5. หลังติดตั้งเสร็จ

1. เปิดเว็บเบราว์เซอร์เข้า `http://127.0.0.1:8001/`
2. ล็อกอินด้วยชื่อผู้ใช้และรหัสผ่านที่ตั้งไว้ตอนติดตั้ง
3. เมนูหลัก: ภาพรวมเครื่อง, ประวัติการแจ้งเตือน, การตั้งค่า (เกณฑ์แจ้งเตือน, กฎเตือน, รายชื่อ IP ที่อนุญาต, ช่องทางแจ้งเตือน, ระยะเวลาเก็บข้อมูล)

### เปิดเป็นแอปเดสก์ท็อป
รัน `Sentinel Desktop.cmd` หรือคลิกไอคอน **Sentinel** ที่ installer วางไว้บนเดสก์ท็อป แอปจะเปิด dashboard ในหน้าต่างเดสก์ท็อปแยกต่างหาก โดยใช้ Edge WebView2 ที่มากับ Windows และจำสถานะล็อกอินไว้ข้ามการเปิด-ปิด

---

## 6. งานอัตโนมัติที่ลงทะเบียนไว้

| งาน | ทำอะไร | เริ่มเมื่อ |
|------|---------|-----------|
| Server Health Monitor API | รัน FastAPI ที่พอร์ต 8000 | ล็อกอิน |
| Server Health Monitor Dashboard | เสิร์ฟหน้า dashboard ที่พอร์ต 8001 | ล็อกอิน |
| Server Health Monitor Collector | เก็บตัวชี้วัดและตรวจความปลอดภัย (ต้องมีสิทธิ์สูงเพื่ออ่าน Windows Security log) | ล็อกอิน |
| Server Health Monitor Watchdog | ตรวจบริการทุก 5 นาที ถ้าตัวไหนหยุดทำงานจะรีสตาร์ตให้ | ล็อกอิน แล้วทำซ้ำทุก 5 นาที |
| Server Health Monitor Backup | สำรองฐานข้อมูลการตั้งค่า เก็บ 14 วัน | ทุกวัน 03:00 |

---

## 7. การสำรองข้อมูล

งาน Backup สำรอง `backend\monitor.db`, `backend\.env` และ `collector\state\` ไปยัง `backups\<วันที่เวลา>/` โดยเก็บ 14 วันล่าสุด

ฐานข้อมูลตัวชี้วัดและเหตุการณ์ใน MongoDB **ไม่ได้อยู่ในสำเนานี้** ถ้าต้องการเก็บระยะยาวให้สำรอง MongoDB แยกต่างหาก (เช่น `mongodump`)

สำรองด้วยตนเอง:
```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

---

## 8. วิธีถอนการติดตั้ง

รันจาก PowerShell แบบผู้ดูแลระบบ:

```powershell
.\scripts\uninstall_windows_autostart.ps1
```

ถ้าต้องการลบข้อมูลด้วยให้ใส่ `-RemoveData`:

```powershell
.\scripts\uninstall_windows_autostart.ps1 -RemoveData
```

`-RemoveData` จะลบ `data/`, `backend\monitor.db`, `backend\logs/`, `frontend\logs/`, `collector\state/` ด้วย ไม่สามารถกู้คืนได้

---

## 9. ข้อจำกัดที่ทราบ

- API ผูกกับ `127.0.0.1` เท่านั้น เหมาะสำหรับใช้ภายในเครื่อง หากต้องเปิดให้เครื่องอื่นเข้าถึง ให้วาง reverse proxy ที่ทำ TLS (เช่น Caddy หรือ Nginx) ไว้ข้างหน้า และปรับ `CORS_ORIGINS` ให้ตรงกับโดเมนจริง
- ยังไม่มีระบบสิทธิ์หลายบทบาท (RBAC) มีบัญชีแอดมินเพียงบัญชีเดียวที่สร้างตอนติดตั้งครั้งแรก การล็อกอินถูกจำกัดจำนวนครั้งต่อ IP และล็อกบัญชีชั่วคราวเมื่อผิดเกินไป
- Docker Desktop บางเครื่อง (CPU รุ่นเก่าที่ไม่มี AVX, Windows 10 รุ่นเก่า) เปิด engine ไม่ได้ ให้ใช้ MongoDB native service แทน และ MongoDB 5.0+ ใน container ต้องมี CPU ที่รองรับ AVX
- คำสั่ง `python` บน PATH ของบางเครื่องเป็นตัว stub ของ Microsoft Store ซึ่งสร้าง `.venv` ไม่ได้ ให้ติดตั้ง Python จาก python.org ใหม่แล้วเลือก "Add Python to PATH"
- ตัว collector ต้องรันด้วยสิทธิ์สูงจึงจะอ่าน Windows Security log ได้ ถ้าไม่ได้สิทธิ์ ระบบจะรายงานว่าการตรวจ failed login ใช้ไม่ได้

---

## 10. หากติดปัญหา

บันทึกการทำงานอยู่ที่:
- `backend\logs\backend.log`, `backend\logs\backend-errors.log`
- `frontend\logs\dashboard.log`, `frontend\logs\dashboard-errors.log`
- `collector\state\collector.log`
- `logs\watchdog.log`

ตรวจสอบระบบทั้งหมด:
```powershell
.\scripts\run_checks.ps1
```
คำสั่งนี้ตรวจไวยากรณ์ รันหน่วยทดสอบ และตรวจ lint ถ้ามี ruff ติดตั้งไว้

ถ้า API ไม่ตอบ ให้รัน `.\scripts\watchdog.ps1` เพื่อตรวจสอบครั้งเดียวด้วยตนเอง

---

อัปเดตล่าสุด: พร้อมใช้งานในสภาพแวดล้อม Windows เดียวหรือทีมขนาดเล็ก

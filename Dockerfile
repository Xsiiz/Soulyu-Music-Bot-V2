# ใช้ Python 3.9-slim เป็น base image (สามารถเปลี่ยนเวอร์ชัน Python ได้ตามต้องการ)
FROM python:3.9-slim

# ตั้งค่า Environment Variables (เป็นทางเลือก แต่แนะนำ)
ENV PYTHONDONTWRITEBYTECODE 1  # ป้องกัน Python จากการสร้างไฟล์ .pyc
ENV PYTHONUNBUFFERED 1         # ป้องกัน Python จากการ buffering stdout และ stderr

# ติดตั้ง Dependencies ของระบบที่จำเป็น:
# - ffmpeg: สำหรับการประมวลผลเสียง
# - libopus0: สำหรับ Opus audio codec ที่ discord.py ใช้
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libopus0 && \
    # ล้าง cache ของ apt-get เพื่อลดขนาด image
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# กำหนด Working Directory ภายใน Container
WORKDIR /app

# คัดลอกไฟล์ requirements.txt เข้าไปยัง Container
COPY requirements.txt .

# ติดตั้ง Python libraries ที่ระบุใน requirements.txt
# --no-cache-dir เพื่อลดขนาด image โดยไม่เก็บ cache ของ pip
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอกไฟล์โค้ดหลักของบอท (main.py) เข้าไปยัง Container
COPY main.py .

# --- ส่วนเพิ่มเติมสำหรับ cookies.txt (ถ้าคุณใช้งาน) ---
# หากคุณต้องการใช้ 'cookiefile': '/app/cookies.txt' ใน YDL_OPTIONS
# คุณต้องมีไฟล์ cookies.txt และคัดลอกเข้าไปใน image ด้วย
# ให้ uncomment บรรทัดด้านล่างนี้หากต้องการใช้งาน
COPY cookies.txt .
# ----------------------------------------------------

# คำสั่งที่จะรันเมื่อ Container เริ่มทำงาน
CMD ["python", "main.py"]
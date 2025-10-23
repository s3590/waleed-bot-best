# 1. Use an official Python runtime as a parent image
FROM python:3.10-slim

# 2. Set the working directory in the container
WORKDIR /app

# --- بداية التعديل الهام ---
# 3. نسخ جميع ملفات المشروع أولاً وقبل كل شيء
# هذا يضمن وجود main.py و strategies/ عند بدء التشغيل
COPY . .
# --- نهاية التعديل الهام ---

# 4. Install system dependencies required for TA-Lib
# (هذا الجزء يبقى كما هو)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib/ \
    && ./configure --prefix=/usr \
    && make \
    && make install \
    && cd .. \
    && rm -rf ta-lib* \
    && apt-get remove -y build-essential wget \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 5. Install Python dependencies from the copied requirements file
# (هذا الجزء يبقى كما هو، لكنه الآن يعمل على الملف الذي تم نسخه في الخطوة 3)
RUN pip install --no-cache-dir -r requirements.txt

# 6. Expose the port the app runs on (هذا السطر غير ضروري للـ worker ولكن لا يضر)
EXPOSE 10000

# 7. Define the command to run the application 
# (هذا السطر سيتم تجاهله لأن Procfile له الأولوية، ولكن من الجيد إبقاؤه)
CMD ["python", "main.py"]

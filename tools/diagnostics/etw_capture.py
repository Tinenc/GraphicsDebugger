import subprocess
import time
import os

etl_path = r"C:\Users\Administrator\Desktop\cod-run.etl"
xml_path = r"C:\Users\Administrator\Desktop\cod-run.xml"

# Stop existing if any
subprocess.run(["logman", "stop", "codrun", "-ets"], capture_output=True)
if os.path.exists(etl_path):
    os.remove(etl_path)

print("[1] Starting ETW session codrun...")
subprocess.run(["logman", "start", "codrun", "-p", "Microsoft-Windows-Kernel-Process", "0x40", "5", "-o", etl_path, "-ets"], check=True)

print("[2] Launching Steam URI steam://rungameid/1938090...")
os.system("start steam://rungameid/1938090")

print("[3] Capturing for 25 seconds...")
for i in range(25):
    time.sleep(1)

print("[4] Stopping ETW session...")
subprocess.run(["logman", "stop", "codrun", "-ets"], check=True)

print("[5] Converting ETL to XML with tracerpt...")
subprocess.run(["tracerpt", etl_path, "-o", xml_path, "-of", "XML", "-y"], check=True)

print("[6] Parsing XML with parse_etw_images.py...")
parser_script = r"C:\Program Files\GraphicsDebugger-Steam-Cod\artifacts\steam-cod-recon\parse_etw_images.py"
res = subprocess.run([r"C:\Users\Administrator\AppData\Local\Python\bin\python.exe", parser_script, xml_path], capture_output=True, text=True)
print(res.stdout)
if res.stderr:
    print("Stderr:", res.stderr)

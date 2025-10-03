import requests
import json
import time
import argparse
from typing import Dict
from pathlib import Path

def print_separator(title: str):
    """Imprime un separador visual para la salida."""
    print(f"\n{'='*25} {title.upper()} {'='*25}")

def print_response(response: requests.Response):
    """Imprime de forma legible la respuesta de una solicitud."""
    print(f"-> Código de Estado: {response.status_code}")
    try:
        print("-> Respuesta JSON:")
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(f"-> Respuesta (No-JSON): {response.text}")

# pa qu sirve esto
def main(base_url: str, json_file_path: str, timeout: int, poll_interval: int, resume_id: str = None):
    """
    Función principal que envía una solicitud desde un archivo JSON y monitorea el resultado.
    """
    print(f"🎯 Apuntando al servidor en: {base_url}")
    
    if resume_id:
        print_separator(f"Reanudando monitoreo para la consulta '{resume_id}'")
        consulta_id = resume_id
    else:
        # --- 1. Cargar la solicitud desde el archivo JSON ---
        if not json_file_path:
            print("❌ Error: Se debe proporcionar un archivo JSON si no se está reanudando una consulta.")
            return
        
        print_separator(f"Cargando solicitud desde {json_file_path}")
        try:
            with open(json_file_path, 'r') as f:
                request_data = json.load(f)
            print("Solicitud cargada exitosamente.")
            print(json.dumps(request_data, indent=2, ensure_ascii=False))
        except FileNotFoundError:
            print(f"❌ Error: El archivo '{json_file_path}' no fue encontrado.")
            return
        except json.JSONDecodeError:
            print(f"❌ Error: El archivo '{json_file_path}' no contiene un JSON válido.")
            return

        # --- 2. Validar la solicitud ---
        print_separator("Paso 1: Validando la solicitud")
        try:
            response = requests.post(f"{base_url}/validate", json=request_data)
            print_response(response)
            if response.status_code != 200:
                print("\n❌ La validación falló. Abortando.")
                return
        except requests.ConnectionError as e:
            print(f"❌ Error de conexión: No se pudo conectar a {base_url}. ¿Está el servidor corriendo?")
            return

        # --- 3. Crear la consulta ---
        print_separator("Paso 2: Creando la consulta")
        response = requests.post(f"{base_url}/query", json=request_data)
        print_response(response)
        if response.status_code != 200:
            print("\n❌ La creación de la consulta falló. Abortando.")
            return
        
        consulta_id = response.json().get("consulta_id")
        if not consulta_id:
            print("\n❌ No se recibió un ID de consulta. Abortando.")
            return
    
    # --- 4. Monitorear el estado de la consulta ---
    print_separator(f"Paso 3: Monitoreando la consulta '{consulta_id}'")
    start_time = time.time()
    final_status = None
    while time.time() - start_time < timeout:
        response = requests.get(f"{base_url}/query/{consulta_id}")
        if response.status_code == 200:
            data = response.json()
            estado = data.get("estado")
            progreso = data.get("progreso")
            mensaje = data.get("mensaje")
            print(f"-> Estado: {estado} | Progreso: {progreso}% | Mensaje: {mensaje}")

            if estado in ["completado", "error"]:
                final_status = estado
                break
        else:
            print(f"-> Error al obtener estado: {response.status_code}")
        
        time.sleep(poll_interval)

    if not final_status:
        print("\n⏰ Timeout esperando la finalización de la consulta.")
        return

    # --- 5. Obtener los resultados finales ---
    if final_status == "completado":
        print_separator("Paso 4: Obteniendo resultados finales")
        response = requests.get(f"{base_url}/query/{consulta_id}?resultados=True")
        print_response(response)
    else:
        print_separator("Consulta finalizada con error")
        print("No se pueden obtener resultados.")

def test_query_local_success(monkeypatch):
    """Simula recuperación exitosa solo desde Lustre/local."""
    monkeypatch.setattr("recover.LustreRecoverFiles.discover_and_filter_files", lambda self, q: [Path("/tmp/fake1.tgz")])
    monkeypatch.setattr("recover.LustreRecoverFiles.scan_existing_files", lambda self, files, dest: files)
    monkeypatch.setattr("recover._process_safe_recover_file", lambda *a, **kw: [Path("/tmp/fake1.tgz")])
    monkeypatch.setattr("recover.S3RecoverFiles.discover_files", lambda *a, **kw: {})
    monkeypatch.setattr("recover.S3RecoverFiles.download_files", lambda *a, **kw: ([], []))

    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200
    consulta_id = response.json()["consulta_id"]

    # Polling hasta completado
    for _ in range(10):
        status = client.get(f"/query/{consulta_id}")
        if status.json()["estado"] == "completado":
            break
        time.sleep(0.1)
    assert status.json()["estado"] == "completado"

def test_query_s3_success(monkeypatch):
    """Simula recuperación exitosa solo desde S3."""
    monkeypatch.setattr("recover.LustreRecoverFiles.discover_and_filter_files", lambda self, q: [])
    monkeypatch.setattr("recover.S3RecoverFiles.discover_files", lambda self, q, d: {"fake2.tgz": "s3://bucket/fake2.tgz"})
    monkeypatch.setattr("recover.S3RecoverFiles.filter_files_by_time", lambda self, files, f, h: files)
    monkeypatch.setattr("recover.S3RecoverFiles.download_files", lambda self, cid, files, dest, db: ([Path("/tmp/fake2.tgz")], []))

    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200
    consulta_id = response.json()["consulta_id"]

    for _ in range(10):
        status = client.get(f"/query/{consulta_id}")
        if status.json()["estado"] == "completado":
            break
        time.sleep(0.1)
    assert status.json()["estado"] == "completado"

def test_query_local_and_s3_fallback(monkeypatch):
    """Simula recuperación mixta: algunos archivos locales, otros desde S3."""
    # Un archivo local, uno solo en S3
    monkeypatch.setattr("recover.LustreRecoverFiles.discover_and_filter_files", lambda self, q: [Path("/tmp/fake1.tgz"), Path("/tmp/fake2.tgz")])
    monkeypatch.setattr("recover.LustreRecoverFiles.scan_existing_files", lambda self, files, dest: [Path("/tmp/fake1.tgz")])
    monkeypatch.setattr("recover._process_safe_recover_file", lambda *a, **kw: [Path("/tmp/fake1.tgz")])
    monkeypatch.setattr("recover.S3RecoverFiles.discover_files", lambda self, q, d: {"fake2.tgz": "s3://bucket/fake2.tgz"})
    monkeypatch.setattr("recover.S3RecoverFiles.filter_files_by_time", lambda self, files, f, h: files)
    monkeypatch.setattr("recover.S3RecoverFiles.download_files", lambda self, cid, files, dest, db: ([Path("/tmp/fake2.tgz")], []))

    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200
    consulta_id = response.json()["consulta_id"]

    for _ in range(10):
        status = client.get(f"/query/{consulta_id}")
        if status.json()["estado"] == "completado":
            break
        time.sleep(0.1)
    assert status.json()["estado"] == "completado"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cliente para la API de solicitudes históricas.")
    parser.add_argument("base_url", help="URL base de la API (ej. http://localhost:9041).")
    parser.add_argument("json_file", nargs='?', default=None, help="Ruta al archivo JSON de la solicitud (requerido si no se usa --resume).")
    parser.add_argument("--resume", type=str, default=None, help="ID de una consulta existente para reanudar el monitoreo.")
    parser.add_argument("--timeout", type=int, default=600, help="Tiempo máximo de espera en segundos para la consulta.")
    parser.add_argument("--poll-interval", type=int, default=10, help="Intervalo en segundos entre cada sondeo de estado.")

    args = parser.parse_args()

    main(args.base_url, args.json_file, args.timeout, args.poll_interval, args.resume)
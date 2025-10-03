import os
import logging
import shutil
import tarfile
from typing import List, Dict, Iterable, Optional
from datetime import datetime, timezone
from pathlib import Path
from pebble import ProcessPool, ThreadPool
from concurrent.futures import TimeoutError
from database import ConsultasDatabase
from collections import defaultdict
import time
from s3_recover import S3RecoverFiles


def filter_files_by_time(archivos_nc: list, fecha_jjj: str, horarios_list: list) -> list:
    """
    Filtra archivos NetCDF por fecha juliana y rango horario.
    Compatible con archivos S3 (string) y rutas locales NetCDF.
    """
    archivos_filtrados = []
    for archivo in archivos_nc:
        nombre = archivo.name if hasattr(archivo, "name") else archivo
        s_idx = nombre.find('_s')
        e_idx = nombre.find('_e')
        if s_idx == -1 or e_idx == -1:
            continue
        ts_str = nombre[s_idx+2:e_idx]  # Ej: '20211211900163'
        if len(ts_str) < 9:
            continue
        anio = ts_str[:4]
        dia_juliano = ts_str[4:7]
        hora = ts_str[7:9]
        if anio + dia_juliano != fecha_jjj:
            continue
        for horario_str in horarios_list:
            partes = horario_str.split('-')
            inicio_hh = partes[0][:2]
            fin_hh = partes[1][:2] if len(partes) > 1 else inicio_hh
            if inicio_hh <= hora <= fin_hh:
                archivos_filtrados.append(archivo)
                break
    return archivos_filtrados

# --- Clase para recuperación local (Lustre) ---
class LustreRecoverFiles:
    def __init__(self, source_data_path: str, logger):
        self.source_data_path = Path(source_data_path)
        self.logger = logger

    def build_base_path(self, query_dict: Dict) -> Path:
        base_path = self.source_data_path
        base_path /= query_dict.get('sensor', 'abi').lower()
        base_path /= query_dict.get('nivel', 'l1b').lower()
        if query_dict.get('dominio'):
            base_path /= query_dict['dominio'].lower()
        return base_path

    def find_files_for_day(self, base_path: Path, fecha_jjj: str) -> List[Path]:
        anio = fecha_jjj[:4]
        dia_del_anio_int = int(fecha_jjj[4:])
        semana = (dia_del_anio_int - 1) // 7 + 1
        directorio_semana = base_path / anio / f"{semana:02d}"
        if not directorio_semana.exists():
            self.logger.warning(f"⚠️ Directorio no encontrado en Lustre: {directorio_semana}")
            return []
        patron_dia = f"*{anio}{dia_del_anio_int:03d}*.tgz"
        archivos_candidatos = list(directorio_semana.glob(patron_dia))
        self.logger.debug(f"  Directorio: {directorio_semana}, Candidatos para el día {fecha_jjj}: {len(archivos_candidatos)}")
        return archivos_candidatos

    def filter_files_by_time(self, archivos_candidatos: List[Path], fecha_jjj: str, horarios_list: List[str]) -> List[Path]:
        archivos_filtrados_dia = []
        for horario_str in horarios_list:
            partes = horario_str.split('-')
            inicio_hhmm = partes[0].replace(':', '')
            fin_hhmm = partes[1].replace(':', '') if len(partes) > 1 else inicio_hhmm
            inicio_ts_str = f"{fecha_jjj}{inicio_hhmm[:2]}00"
            fin_ts_str = f"{fecha_jjj}{fin_hhmm[:2]}59"
            try:
                inicio_ts = int(inicio_ts_str)
                fin_ts = int(fin_ts_str)
            except ValueError:
                self.logger.warning(f"Formato de timestamp inválido para {fecha_jjj} con horario {horario_str}. Se omite.")
                continue
            self.logger.debug(f"    Filtrando por rango horario: {horario_str} ({inicio_ts} - {fin_ts})")
            for archivo in archivos_candidatos:
                try:
                    s_part_start_idx = archivo.name.find('-s')
                    if s_part_start_idx != -1:
                        file_ts_str = archivo.name[s_part_start_idx + 2 : s_part_start_idx + 13]
                        file_ts = int(file_ts_str)
                        if inicio_ts <= file_ts <= fin_ts:
                            archivos_filtrados_dia.append(archivo)
                except (ValueError, IndexError, AttributeError):
                    continue
        return archivos_filtrados_dia

    def discover_and_filter_files(self, query_dict: Dict) -> List[Path]:
        archivos_encontrados_set = set()
        base_path = self.build_base_path(query_dict)
        for fecha_jjj, horarios_list in query_dict.get('fechas', {}).items():
            archivos_candidatos_dia = self.find_files_for_day(base_path, fecha_jjj)
            if not archivos_candidatos_dia:
                continue
            archivos_filtrados = self.filter_files_by_time(archivos_candidatos_dia, fecha_jjj, horarios_list)
            archivos_encontrados_set.update(archivos_filtrados)
        return sorted(list(archivos_encontrados_set))

    def scan_existing_files(self, archivos_a_procesar: List[Path], destino: Path) -> List[Path]:
        if not destino.exists() or not any(destino.iterdir()):
            return archivos_a_procesar
        self.logger.info(f"🔍 Escaneando {destino} en busca de archivos ya recuperados...")
        timestamps_existentes = set()
        for f in destino.iterdir():
            if f.is_file():
                s_part_start_idx = f.name.find('_s')
                if s_part_start_idx != -1:
                    timestamp_part = f.name[s_part_start_idx + 2 : s_part_start_idx + 13]
                    timestamps_existentes.add(timestamp_part)
        archivos_pendientes = []
        for archivo_fuente in archivos_a_procesar:
            s_part_start_idx = archivo_fuente.name.find('_s')
            if s_part_start_idx != -1:
                timestamp_fuente = archivo_fuente.name[s_part_start_idx + 2 : s_part_start_idx + 13]
                if timestamp_fuente not in timestamps_existentes:
                    archivos_pendientes.append(archivo_fuente)
            else:
                archivos_pendientes.append(archivo_fuente)
        num_recuperados = len(archivos_a_procesar) - len(archivos_pendientes)
        self.logger.info(f"📊 Escaneo completo. {num_recuperados} archivos ya recuperados, {len(archivos_pendientes)} pendientes.")
        return archivos_pendientes


# --- Clase principal orquestadora ---
class RecoverFiles:
    def __init__(self, db: ConsultasDatabase, source_data_path: str, base_download_path: str, executor, s3_fallback_enabled: Optional[bool] = None, lustre_enabled: Optional[bool] = None, max_workers: Optional[int] = None):
        self.db = db
        self.source_data_path = Path(source_data_path)
        self.base_download_path = Path(base_download_path)
        self.logger = logging.getLogger(__name__)
        self.executor = executor
        self.s3_fallback_enabled = (s3_fallback_enabled
                                    if s3_fallback_enabled is not None
                                    else os.getenv("S3_FALLBACK_ENABLED", "1") not in ("0", "false", "False"))
        # Nuevo flag para Lustre
        if lustre_enabled is not None:
            self.lustre_enabled = lustre_enabled
        else:
            disable_lustre_env = os.getenv("DISABLE_LUSTRE", "").lower()
            self.lustre_enabled = os.getenv("LUSTRE_ENABLED", "1") not in ("0", "false", "False") and disable_lustre_env not in ("1", "true")

        self.FILE_PROCESSING_TIMEOUT_SECONDS = int(os.getenv("FILE_PROCESSING_TIMEOUT_SECONDS", "120"))
        self.S3_RETRY_ATTEMPTS = 3
        self.S3_RETRY_BACKOFF_SECONDS = 2
        self.GOES19_OPERATIONAL_DATE = datetime(2025, 4, 1, tzinfo=timezone.utc)
        self.lustre = LustreRecoverFiles(source_data_path, self.logger)

        # Inicializa self.max_workers ANTES de usarla
        self.max_workers = (
            max_workers
            or getattr(executor, "max_workers", None)
            or int(os.getenv("HISTORIC_MAX_WORKERS", "4"))
        )

        self.s3 = S3RecoverFiles(self.logger, self.max_workers, self.S3_RETRY_ATTEMPTS, self.S3_RETRY_BACKOFF_SECONDS)

    def procesar_consulta(self, consulta_id: str, query_dict: Dict):
        try:
            self.logger.info(f" Atendiendo solicitud {consulta_id}")

            # 1. Preparar entorno
            directorio_destino = self.base_download_path / consulta_id
            directorio_destino.mkdir(exist_ok=True, parents=True)
            self.db.actualizar_estado(consulta_id, "procesando", 10, "Preparando entorno")

            # 2. Descubrir y filtrar archivos locales
            archivos_a_procesar_local = self.lustre.discover_and_filter_files(query_dict)
            inaccessible_files_local = []  # Si tienes lógica para esto, agrégala aquí
            self.logger.info(f"🔎 Se encontraron {len(archivos_a_procesar_local)} archivos potenciales en el almacenamiento local.")

            # 3. Escanear destino
            if archivos_a_procesar_local:
                archivos_pendientes_local = self.lustre.scan_existing_files(archivos_a_procesar_local, directorio_destino)
                if not archivos_pendientes_local:
                    self.logger.info("👍 No hay archivos locales pendientes, todos los encontrados ya fueron recuperados.")
            else:
                archivos_pendientes_local = []

            total_pendientes = len(archivos_pendientes_local)
            self.db.actualizar_estado(consulta_id, "procesando", 20, f"Identificados {total_pendientes} archivos pendientes de procesar.")

            objetivos_fallidos_local = []
            objetivos_fallidos_local.extend(inaccessible_files_local)

            # 4. Procesar archivos pendientes en paralelo
            if archivos_pendientes_local:
                future_to_objetivo = {
                    self.executor.schedule(
                        _process_safe_recover_file, 
                        args=(
                            archivo_a_procesar, 
                            directorio_destino, 
                            query_dict.get('nivel'), 
                            query_dict.get('productos'), 
                            query_dict.get('bandas')
                        ), 
                        timeout=self.FILE_PROCESSING_TIMEOUT_SECONDS
                    ): archivo_a_procesar
                    for i, archivo_a_procesar in enumerate(archivos_pendientes_local)
                }
                for i, future in enumerate(future_to_objetivo.keys()):
                    archivo_fuente = future_to_objetivo[future]
                    progreso = 20 + int(((i + 1) / total_pendientes) * 60)
                    self.db.actualizar_estado(consulta_id, "procesando", progreso, f"Procesando archivo {i+1}/{total_pendientes}")
                    try:
                        future.result()
                    except TimeoutError:
                        self.logger.error(f"❌ Procesamiento del archivo {archivo_fuente.name} excedió el tiempo límite de {self.FILE_PROCESSING_TIMEOUT_SECONDS}s y fue terminado.")
                        objetivos_fallidos_local.append(archivo_fuente)
                    except Exception as e:
                        self.logger.error(f"❌ Error procesando el archivo {archivo_fuente.name}: {e}")
                        objetivos_fallidos_local.append(archivo_fuente)

            # 5. Recuperar desde S3 si está habilitado
            if self.s3_fallback_enabled:
                self.db.actualizar_estado(consulta_id, "procesando", 85, "Buscando archivos adicionales en S3.")
                # Separar productos CMI* de no-CMI para no aplicar 'bandas' a ACHA/otros
                productos_req = (query_dict.get("productos") or [])
                productos_upper = [str(p).strip().upper() for p in productos_req]
                cmi_products = [p for p in productos_upper if p.startswith("CMI")]
                other_products = [p for p in productos_upper if not p.startswith("CMI")]

                s3_map = {}
                # Consulta para CMI*: respeta 'bandas'
                if cmi_products:
                    q_cmi = dict(query_dict)
                    q_cmi["productos"] = cmi_products
                    q_cmi["bandas"] = query_dict.get("bandas") or []
                    s3_map.update(self.s3.discover_files(q_cmi, self.GOES19_OPERATIONAL_DATE))
                # Consulta para no-CMI: ignorar 'bandas'
                if other_products:
                    q_other = dict(query_dict)
                    q_other["productos"] = other_products
                    q_other["bandas"] = []  # explícito: no filtrar por banda
                    s3_map.update(self.s3.discover_files(q_other, self.GOES19_OPERATIONAL_DATE))

                archivos_s3_filtrados = []
                for fecha_jjj, horarios_list in query_dict.get('fechas', {}).items():
                    archivos_encontrados = [s3_map[k] for k in s3_map]
                    archivos_s3_filtrados += filter_files_by_time(archivos_encontrados, fecha_jjj, horarios_list)
                objetivos_finales_s3 = list(set(archivos_s3_filtrados))
                s3_recuperados, objetivos_fallidos_final = self.s3.download_files(
                    consulta_id, objetivos_finales_s3, directorio_destino, self.db
                )
            else:
                s3_recuperados = []
                objetivos_fallidos_final = objetivos_fallidos_local

            # 6. Generar reporte final
            all_files_in_destination = [f for f in directorio_destino.iterdir() if f.is_file()]
            self.db.actualizar_estado(consulta_id, "procesando", 95, "Generando reporte final")
            resultados_finales = self._generar_reporte_final(
                consulta_id, all_files_in_destination, s3_recuperados, directorio_destino, objetivos_fallidos_final, query_dict
            )
            self.db.guardar_resultados(consulta_id, resultados_finales)
            self.logger.info(f"✅ Procesamiento completado para {consulta_id}")

        except Exception as e:
            self.logger.error(f"❌ Error procesando consulta {consulta_id}: {e}")
            self.db.actualizar_estado(consulta_id, "error", 0, f"Error: {str(e)}")

    def _build_recovery_query(self, consulta_id: str, objetivos_fallidos: List[Path], query_original: Dict) -> Optional[Dict]:
        """Construye una nueva consulta a partir de los archivos que fallaron."""
        if not objetivos_fallidos:
            return None

        fechas_fallidas = defaultdict(list)
        original_fechas = query_original.get('_original_request', {}).get('fechas', {})

        for archivo_fallido in objetivos_fallidos:
            try:
                # 1. Extraer el timestamp YYYYJJJHHMM del nombre del archivo.
                ts_str = archivo_fallido.name.split('-s')[1].split('.')[0][:11]
                fecha_fallida_dt = datetime.strptime(ts_str, '%Y%j%H%M')
                fecha_fallida_ymd = fecha_fallida_dt.strftime('%Y%m%d')

                # 2. Encontrar la clave de fecha y el rango horario originales.
                for fecha_key_original, horarios_list in original_fechas.items():
                    # Comprobar si la fecha del archivo está dentro del rango de la clave (ej. "20230101-20230105")
                    start_date_str = fecha_key_original.split('-')[0]
                    end_date_str = fecha_key_original.split('-')[-1]
                    if not (start_date_str <= fecha_fallida_ymd <= end_date_str):
                        continue

                    for horario_rango in horarios_list:
                        # Comprobar si la hora del archivo está dentro del rango horario.
                        inicio_str, fin_str = (horario_rango.split('-') + [horario_rango])[:2]
                        inicio_t = datetime.strptime(inicio_str, '%H:%M').time()
                        fin_t = datetime.strptime(fin_str, '%H:%M').time()

                        if inicio_t <= fecha_fallida_dt.time() <= fin_t:
                            if horario_rango not in fechas_fallidas[fecha_key_original]:
                                fechas_fallidas[fecha_key_original].append(horario_rango)
                            break # Encontrado el rango horario, pasar al siguiente archivo.
                    else:
                        continue
                    break # Encontrada la clave de fecha, pasar al siguiente archivo.

            except (IndexError, ValueError):
                continue

        if fechas_fallidas:
            consulta_recuperacion = query_original.get('_original_request', {}).copy()
            consulta_recuperacion.pop('creado_por', None)
            consulta_recuperacion['fechas'] = dict(fechas_fallidas)
            consulta_recuperacion['descripcion'] = f"Consulta de recuperación para la solicitud original {consulta_id}"
            return consulta_recuperacion
        
        return None

    def _generar_reporte_final(self, consulta_id: str, all_files_in_destination: List[Path], s3_recuperados: List[Path], directorio_destino: Path, objetivos_fallidos: List[Path], query_original: Dict) -> Dict:
        """Genera el diccionario de resultados finales."""
        lustre_files_for_report = [f for f in all_files_in_destination if f not in s3_recuperados]
        todos_los_archivos = all_files_in_destination
        total_bytes = sum(f.stat().st_size for f in todos_los_archivos if f.is_file())
        tamaño_mb = round(total_bytes / (1024 * 1024), 2)

        # Construir la consulta de recuperación usando el método refactorizado.
        consulta_recuperacion = self._build_recovery_query(consulta_id, objetivos_fallidos, query_original)

        return {
            "fuentes": {
                "lustre": {
                    "archivos": [f.name for f in lustre_files_for_report],
                    "total": len(lustre_files_for_report)
                },
                "s3": {
                    "archivos": [f.name for f in s3_recuperados],
                    "total": len(s3_recuperados)
                }
            },
            "total_archivos": len(todos_los_archivos),
            "tamaño_total_mb": tamaño_mb,
            "directorio_destino": str(directorio_destino),
            "timestamp_procesamiento": datetime.now().isoformat(),
            "consulta_recuperacion": consulta_recuperacion
        }

    def _producto_requiere_bandas(self, nivel: str, producto: str) -> bool:
        n = (nivel or "").strip().upper()
        p = (producto or "").strip().upper()
        return n == "L1B" or (n == "L2" and p.startswith("CMI"))

    def _iter_patrones_l2(
        self,
        productos: List[str],
        dominio: str,
        bandas: List[str],
        sat_code: str,
        ts_inicio: str,
        ts_fin: str,
    ) -> Iterable[str]:
        """
        Genera patrones de filename para L2.
        - CMI/CMIP/CMIPC: incluye banda como M6Cdd
        - Otros (p.ej. ACHA/ACTP): sin banda (solo M6)
        """
        dom_letter = "C" if dominio == "conus" else "F"
        productos_upper = [p.strip().upper() for p in (productos or [])]
        bandas = bandas or []

        for prod in productos_upper:
            if prod.startswith("CMI"):
                # Si no se enviaron bandas, usa ALL (16) o deja que aguas abajo expanda.
                iter_bandas = bandas or [f"{i:02d}" for i in range(1, 17)]
                for b in iter_bandas:
                    b2 = f"{int(b):02d}" if str(b).isdigit() else str(b)
                    # Ejemplo: CG_ABI-L2-CMIPC-M6C13_G16_sYYYYJJJHHMMSS_eYYYYJJJHHMMSS_c*.nc
                    yield f"CG_ABI-L2-{prod}{dom_letter}-M6C{b2}_{sat_code}_s{ts_inicio}_e{ts_fin}_c*.nc"
            else:
                # Ejemplo: CG_ABI-L2-ACHAC-M6_G16_sYYYYJJJHHMMSS_eYYYYJJJHHMMSS_c*.nc
                yield f"CG_ABI-L2-{prod}{dom_letter}-M6_{sat_code}_s{ts_inicio}_e{ts_fin}_c*.nc"

    # En donde actualmente construyes los patrones para buscar en Lustre/S3,
    # reemplaza el bloque que usa 'bandas' para todos los productos por algo como:
    def _construir_patrones_busqueda(self, query: Dict) -> List[str]:
        """
        Construye los patrones de archivo a buscar según la query.
        """
        nivel = (query.get("nivel") or "").upper()
        dominio = query.get("dominio")
        productos = query.get("productos") or []
        sat_code = self._sat_to_code(query.get("sat"))  # asume que existe
        ts_inicio, ts_fin = self._rangos_a_timestamps(query)  # asume que existe

        patrones: List[str] = []

        if nivel == "L1B":
            # Mantén tu lógica actual para L1B con bandas
            # ...existing code...
            pass
        elif nivel == "L2":
            # USAR patrones por producto (bandas solo para CMI)
            patrones.extend(
                list(
                    self._iter_patrones_l2(
                        productos=productos,
                        dominio=dominio,
                        bandas=query.get("bandas") or [],
                        sat_code=sat_code,
                        ts_inicio=ts_inicio,
                        ts_fin=ts_fin,
                    )
                )
            )
        else:
            # ...existing code...
            pass

        logging.debug(f"Patrones L2 generados: {patrones}")
        return patrones

# --- Funciones a nivel de módulo para ProcessPoolExecutor ---
# ProcessPoolExecutor requiere que las funciones que se ejecutan en otros procesos
# estén definidas a nivel superior del módulo, no como métodos de una clase.

def _process_safe_recover_file(archivo_fuente: Path, directorio_destino: Path, nivel: str, productos_solicitados_list: List[str], bandas_solicitadas_list: List[str]) -> List[Path]:
    """
    Función segura para procesos que procesa un único archivo .tgz.
    Verifica accesibilidad, y luego lo copia o extrae su contenido según la consulta.
    """
    archivos_recuperados = []
    
    try:
        with tarfile.open(archivo_fuente, "r:gz") as tar:
            productos_en_tgz = set()
            bandas_en_tgz = set()
            miembros_del_tar = tar.getmembers()

            for miembro in miembros_del_tar:
                if miembro.isfile():
                    if "-L2-" in miembro.name:
                        try: productos_en_tgz.add(miembro.name.split('-L2-')[1].split('F-')[0])
                        except IndexError: pass
                    if "C" in miembro.name and "_" in miembro.name:
                        try:
                            banda = miembro.name.split('C', 1)[1].split('_', 1)[0]
                            if banda.isdigit():
                                bandas_en_tgz.add(banda)
                        except IndexError: pass

            productos_solicitados = set(productos_solicitados_list or [])
            bandas_solicitadas = set(bandas_solicitadas_list or [])

            copiar_tgz_completo = (nivel == 'L2' and not productos_solicitados) or \
                                  (nivel == 'L1b' and not bandas_solicitadas) or \
                                  (nivel == 'L2' and productos_solicitados and not productos_en_tgz.issubset(productos_solicitados)) or \
                                  (nivel == 'L1b' and bandas_solicitadas and not bandas_en_tgz.issubset(bandas_solicitadas))

            if copiar_tgz_completo:
                logging.debug(f"📦 Copiando archivo completo (contenido mixto): {archivo_fuente.name}")
                shutil.copy(archivo_fuente, directorio_destino)
                archivos_recuperados.append(directorio_destino / archivo_fuente.name)
                return archivos_recuperados

            miembros_a_extraer = []
            for miembro in miembros_del_tar:
                if any(f"-L2-{p}" in miembro.name for p in productos_solicitados) or \
                   any(f"C{b}_" in miembro.name for b in bandas_solicitadas):
                    miembros_a_extraer.append(miembro)
            
            if miembros_a_extraer:
                logging.debug(f"🔎 Extrayendo {len(miembros_a_extraer)} archivos de {archivo_fuente.name}")
                tar.extractall(path=directorio_destino, members=miembros_a_extraer)
                for miembro in miembros_a_extraer:
                    archivos_recuperados.append(directorio_destino / miembro.name)
            
            if not miembros_a_extraer:
                raise FileNotFoundError(f"No se encontraron archivos internos que coincidieran con la solicitud en {archivo_fuente.name}")

    except (tarfile.ReadError, tarfile.ExtractError, FileNotFoundError) as e:
        logging.error(f"❌ Error al procesar el archivo tar {archivo_fuente.name} (posiblemente corrupto): {e}")
        raise

    return archivos_recuperados
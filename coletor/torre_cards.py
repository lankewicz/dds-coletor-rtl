"""Regras de cards portadas do DDS em 10/10/2026; sem acesso a Firestore/Storage.
Origem: monitor/services/torre_monitor_builder.py e auxiliares de presença.
A composição deve fornecer todas as entradas locais explicitamente.
"""
from __future__ import annotations
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo
DDS_TIMEZONE = "America/Sao_Paulo"

def to_utc_dt(ts: Any):
    if not ts:
        return None
    if hasattr(ts, 'to_datetime'):
        dt = ts.to_datetime()
    elif isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, str):
        text = ts.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except Exception:
            parsed_dt = None
            for fmt in ('%d-%m-%Y - %H:%M', '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d/%m/%Y - %H:%M', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    parsed_dt = datetime.strptime(text, fmt)
                    break
                except Exception:
                    continue
            if parsed_dt is not None:
                dt = parsed_dt
            else:
                return None
    else:
        return None
    if getattr(dt, 'tzinfo', None) is None:
        try:
            tz = ZoneInfo(DDS_TIMEZONE)
        except Exception:
            tz = timezone(timedelta(hours=-3))
        dt = dt.replace(tzinfo=tz).astimezone(timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt

def normalize_estado(value: str | None) -> str:
    raw = (value or 'DESCONHECIDO').strip().upper()
    aliases = {'ESPECIAL': 'DESLOCAMENTO_ESPECIAL', 'DESLOCAMENTO': 'DESLOCAMENTO_ESPECIAL'}
    return aliases.get(raw, raw)

def normalize_rotalog_turn_state(value: str | None) -> str:
    """ROTALOG DESLOCAMENTO é etapa do serviço, nunca deslocamento especial."""
    raw = (value or 'DESCONHECIDO').strip().upper()
    if raw == 'DESLOCAMENTO':
        return 'ABERTO'
    return normalize_estado(raw)

def _normalize_text(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    text = unicodedata.normalize('NFKD', text)
    text = ''.join((ch for ch in text if not unicodedata.combining(ch)))
    text = text.upper()
    text = re.sub('\\s+', ' ', text).strip()
    return text

def _normalized_alias_matches(value: str | None, alias: str | None) -> bool:
    """Compara equipe por chave exata ou por token."""
    norm_value = _normalize_text(value)
    norm_alias = _normalize_text(alias)
    if not norm_value or not norm_alias:
        return False
    if norm_value == norm_alias:
        return True
    token_re = f'(?<![A-Z0-9]){re.escape(norm_alias)}(?![A-Z0-9])'
    if re.search(token_re, norm_value):
        return True
    return False

def _has_dds_in_recent_map(safe_key: str, recent_dds_teams: dict[str, dict[str, Any]]) -> tuple[bool, dict[str, Any] | None]:
    if not recent_dds_teams:
        return (False, None)
    if safe_key in recent_dds_teams:
        return (True, recent_dds_teams[safe_key])
    for t_code, entry_val in recent_dds_teams.items():
        if _normalized_alias_matches(t_code, safe_key):
            return (True, entry_val)
    return (False, None)

def _extract_completed_time(completed_at: str | None) -> str | None:
    if not completed_at:
        return None
    try:
        text = str(completed_at).strip()
        if 'T' in text:
            return text.split('T')[1][:5]
        parts = text.split()
        if len(parts) > 1 and ':' in parts[1]:
            return parts[1][:5]
        return None
    except Exception:
        return None

def get_team_dds_presence(team_key: str, available_days: list[str], projections_by_day: dict[str, dict[str, Any]], today_iso: str) -> dict[str, Any]:
    """
    Retorna o dicionário com ddsDays, ddsHistory, ddsTimes, ddsPhotos e ddsToday para a equipe.
    """
    safe_key = str(team_key or '').strip().upper()
    history: list[str] = []
    times: dict[str, str] = {}
    has_today = False
    for day in available_days:
        day_entry = projections_by_day.get(day, {})
        teams_map = day_entry.get('teams', {})
        matched_entry = teams_map.get(safe_key)
        if not matched_entry:
            for t_code, entry_val in teams_map.items():
                if _normalized_alias_matches(t_code, safe_key):
                    matched_entry = entry_val
                    break
        if matched_entry and isinstance(matched_entry, dict):
            history.append('ok')
            c_time = _extract_completed_time(matched_entry.get('completedAt'))
            if c_time:
                times[day] = c_time
            if day == today_iso:
                has_today = True
        else:
            history.append('fail')
    return {'ddsDays': available_days, 'ddsHistory': history, 'ddsTimes': times, 'ddsPhotos': {}, 'ddsToday': 'ok' if has_today else 'neutral'}

def _build_pure_torre_items(rotalog_snapshots: dict[str, dict[str, Any]], now: datetime, teams_map: dict[str, dict[str, Any]] | None=None, recent_7d_dds: dict[str, dict[str, Any]] | None=None, *, manual_refresh: bool=False, daily_projections: tuple[list[str], dict[str, dict[str, Any]]] | None=None) -> list[dict[str, Any]]:
    """
    Constrói os cards de equipes com telemetria a partir do snapshot consolidado da Torre de Controle.
    """
    items = []
    now_local = now.astimezone(ZoneInfo(DDS_TIMEZONE)) if DDS_TIMEZONE else now
    today_iso = now_local.date().isoformat()
    teams_map = teams_map or {}
    recent_7d_dds = recent_7d_dds or {}
    if daily_projections is None:
        raise ValueError('Fornecer todas as entradas locais da torre')
    available_days, projections_by_day = daily_projections
    for team_key, snapshot in rotalog_snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        safe_key = str(snapshot.get('teamKey') or team_key or '').strip().upper()
        if not safe_key:
            continue
        team_data = teams_map.get(safe_key) or teams_map.get(team_key) or {}
        is_v2 = 'conexao' in snapshot or 'jornada' in snapshot or 'ordensServico' in snapshot
        conexao = snapshot.get('conexao') if isinstance(snapshot.get('conexao'), dict) else {}
        jornada = snapshot.get('jornada') if isinstance(snapshot.get('jornada'), dict) else {}
        ordens = snapshot.get('ordensServico') if isinstance(snapshot.get('ordensServico'), dict) else {}
        turno = jornada.get('turno') if is_v2 else snapshot.get('turno') or {}
        if not isinstance(turno, dict):
            turno = {}
        activity = ordens.get('atual') if is_v2 else snapshot.get('atividadeAtual') or {}
        if not isinstance(activity, dict):
            activity = {}
        colaborador_str = str(conexao.get('colaborador') or snapshot.get('colaborador') or '').strip()
        participantes = [p.strip() for p in colaborador_str.split('/') if p.strip()] if colaborador_str else []
        if not participantes:
            cad_members = team_data.get('members')
            if isinstance(cad_members, list):
                participantes = [str(m).strip() for m in cad_members if m]
            elif isinstance(cad_members, str):
                participantes = [m.strip() for m in cad_members.split(',') if m.strip()]
        colaborador = colaborador_str or (participantes[0] if participantes else None)
        motorista = team_data.get('motorista') or (participantes[0] if participantes else None)
        coringas = team_data.get('coringas') or []
        team_type = team_data.get('teamType') or snapshot.get('teamType')
        equipe_nome = team_data.get('displayName') or safe_key
        veiculo = str(conexao.get('veiculo') or snapshot.get('veiculo') or team_data.get('veiculo') or '').strip()
        equipamento = str(conexao.get('identificadorEquipamento') or snapshot.get('identificadorEquipamento') or team_data.get('tablet') or '').strip()
        tablet = equipamento or (veiculo if conexao.get('origemResolucao') == 'VEICULO_COM_PREFIXO_EQUIPE' else None) or safe_key
        is_online = bool(conexao.get('isOnline') if 'isOnline' in conexao else snapshot.get('isOnline', True))
        status_conexao = str(conexao.get('status') or snapshot.get('statusConexao') or ('online' if is_online else 'offline')).strip()
        turno_status = str(turno.get('status') or snapshot.get('turnStatus') or '').upper()
        em_intervalo = bool(jornada.get('emIntervalo')) if is_v2 else bool(snapshot.get('intervalo', {}).get('inicio') and (not snapshot.get('intervalo', {}).get('fim')))
        turno_inicio = turno.get('inicio') or turno.get('inicio_iso') or turno.get('inicioIso')
        turno_fim = turno.get('fim') or turno.get('fim_iso') or turno.get('fimIso')
        dt_ini = to_utc_dt(turno_inicio) if turno_inicio else None
        dt_fim = to_utc_dt(turno_fim) if turno_fim else None
        dt_ini_local = dt_ini.astimezone(ZoneInfo(DDS_TIMEZONE)) if dt_ini and DDS_TIMEZONE else dt_ini
        protocol = activity.get('protocolo') or activity.get('protocoloBruto') or activity.get('ssId') or activity.get('serviceId') or '-'
        activity_status = str(activity.get('statusAtual') or activity.get('status') or '').upper()
        total_concluidos = int((ordens.get('totalConcluidos') if is_v2 else len(snapshot.get('ssExecutadas') or [])) or 0)
        has_active_order = bool(activity_status in ('EXECUCAO', 'DESLOCAMENTO'))
        updated_dt = to_utc_dt(snapshot.get('updatedAt')) or to_utc_dt(snapshot.get('updatedAtIso')) or to_utc_dt(snapshot.get('lastCollectedAt'))
        updated_dt_local = updated_dt.astimezone(ZoneInfo(DDS_TIMEZONE)) if updated_dt and DDS_TIMEZONE else updated_dt
        horas_aberto = (now_local - dt_ini_local).total_seconds() / 3600.0 if dt_ini_local else 0.0
        horas_sem_sinal = (now_local - updated_dt_local).total_seconds() / 3600.0 if updated_dt_local else 999.0
        sinal_antigo = bool(status_conexao == '> 1h' and horas_sem_sinal >= 6.0)
        if turno_status == 'ABERTO':
            if horas_aberto >= 12.0 and (sinal_antigo or (dt_ini_local and dt_ini_local.date() < now_local.date() and (not is_online))):
                turno_status = 'FECHADO'
                turno_fim = turno_fim or snapshot.get('updatedAt') or f'{dt_ini_local.date().isoformat()}T18:00:00-03:00'
                dt_fim = to_utc_dt(turno_fim)
                has_active_order = False
            elif dt_ini_local and dt_ini_local.date() < now_local.date() and (not has_active_order):
                turno_status = 'FECHADO'
                turno_fim = turno_fim or snapshot.get('updatedAt') or f'{dt_ini_local.date().isoformat()}T18:00:00-03:00'
                dt_fim = to_utc_dt(turno_fim)
            elif dt_ini_local and dt_ini_local.date() == now_local.date() and (dt_ini_local.hour < 6):
                if now_local.hour >= 8 and (not has_active_order) and (not is_online):
                    turno_status = 'FECHADO'
                    turno_fim = turno_fim or snapshot.get('updatedAt') or f'{today_iso}T08:00:00-03:00'
                    dt_fim = to_utc_dt(turno_fim)
        if em_intervalo:
            estado = 'INTERVALO'
        elif turno_status == 'FECHADO':
            estado = 'FECHADO'
        elif turno_status == 'ABERTO':
            estado = 'ABERTO'
        else:
            estado = normalize_rotalog_turn_state(snapshot.get('estadoConsolidado') or turno_status or 'DESCONHECIDO')
        snapshot_date = str(snapshot.get('date') or '').strip()
        updated_at = snapshot.get('updatedAt') or snapshot.get('updatedAtIso') or snapshot.get('lastCollectedAt') or now.isoformat()
        minutos = None
        up_dt = to_utc_dt(updated_at)
        if up_dt:
            minutos = max(0, int((now - up_dt).total_seconds() // 60))
        rotalog_comunicou_7d = False
        if has_active_order:
            rotalog_comunicou_7d = True
        elif up_dt and (now - up_dt).total_seconds() <= 7 * 86400:
            rotalog_comunicou_7d = True
        elif dt_ini and (now - dt_ini).total_seconds() <= 7 * 86400:
            rotalog_comunicou_7d = True
        elif snapshot_date:
            try:
                s_date = datetime.strptime(snapshot_date, '%Y-%m-%d').date()
                if (now_local.date() - s_date).days <= 7:
                    rotalog_comunicou_7d = True
            except Exception:
                pass
        has_recent_dds, recent_dds_entry = _has_dds_in_recent_map(safe_key, recent_7d_dds)
        team_active = bool(rotalog_comunicou_7d or has_recent_dds)
        last_contact = updated_at
        last_contact_source = 'R'
        if has_recent_dds and isinstance(recent_dds_entry, dict):
            dds_recent_ts = recent_dds_entry.get('completedAt')
            if dds_recent_ts:
                dds_dt = to_utc_dt(dds_recent_ts)
                if dds_dt and (not up_dt or dds_dt > up_dt):
                    last_contact = dds_recent_ts
                    last_contact_source = 'D'
        dds_presence = get_team_dds_presence(safe_key, available_days, projections_by_day, today_iso)
        item = {'teamKey': safe_key, 'equipe': equipe_nome, 'veiculo': veiculo or None, 'setor': team_data.get('setor') or 'TODOS', 'estado': estado, 'estadoOriginal': estado, 'origemAtualizacao': 'TORRE_CONTROLE', 'deviceIdLastWriter': 'TORRE_CONTROLE', 'rotalogSnapshot': snapshot, 'tablet': tablet, 'ss': protocol, 'motivo': '-', 'updatedAt': updated_at, 'openedAtClientMs': int(dt_ini.timestamp() * 1000) if dt_ini else None, 'closedAtClientMs': int(dt_fim.timestamp() * 1000) if dt_fim else None, 'turnoInicio': turno_inicio, 'turnoFim': turno_fim, 'turnStatus': turno_status, 'totalConcluidos': total_concluidos, 'ssExecutadasCount': total_concluidos, 'minutosDesdeAtualizacao': minutos, 'critico': False, 'alerta': None, 'participantes': participantes, 'colaborador': colaborador, 'motorista': motorista, 'coringas': coringas, 'active': team_active, 'lastContact': last_contact, 'lastContactSource': last_contact_source, 'isOnline': is_online, 'statusConexao': status_conexao, 'atividadeStatusRotalog': activity_status or None, 'monitorStatusRotalog': activity_status or estado, 'operacional': {'fonte': 'TORRE_CONTROLE', 'disponivel': True, 'estado': estado, 'atividade': activity_status or None, 'servicoAtual': activity if activity else None, 'atualizadoEm': updated_at, 'veiculo': veiculo or None, 'identificadorEquipamento': equipamento or None, 'colaborador': colaborador, 'totalConcluidos': total_concluidos, 'latitude': activity.get('latitude'), 'longitude': activity.get('longitude')}, 'ddsHistory': dds_presence['ddsHistory'], 'ddsToday': dds_presence['ddsToday'], 'ddsDays': dds_presence['ddsDays'], 'ddsTimes': dds_presence['ddsTimes'], 'ddsPhotos': {}, 'unreadMessages': 0, 'unreadMap': {}, 'lastWasDescansoSemanal': False, 'teamType': team_type}
        items.append(item)
    items.sort(key=lambda x: str(x.get('teamKey') or ''))
    return items

def _build_dds_controlled_items(telemetry_keys: set[str], now: datetime, teams_map: dict[str, dict[str, Any]] | None=None, recent_7d_dds: dict[str, dict[str, Any]] | None=None, *, manual_refresh: bool=False, today_dds_teams: dict[str, dict[str, Any]] | None=None, daily_projections: tuple[list[str], dict[str, dict[str, Any]]] | None=None) -> list[dict[str, Any]]:
    """
    Constrói os cards de equipes sem telemetria controladas exclusivamente via JSON do DDS.
    """
    now_local = now.astimezone(ZoneInfo(DDS_TIMEZONE)) if DDS_TIMEZONE else now
    today_iso = now_local.date().isoformat()
    is_after_18 = now_local.hour >= 18
    recent_7d_dds = recent_7d_dds or {}
    if daily_projections is None:
        raise ValueError('Fornecer todas as entradas locais da torre')
    available_days, projections_by_day = daily_projections
    if teams_map is None:
        raise ValueError('Fornecer todas as entradas locais da torre')
    if today_dds_teams is None:
        raise ValueError('Fornecer todas as entradas locais da torre')
    if recent_7d_dds is None:
        raise ValueError('Fornecer todas as entradas locais da torre')
    non_telemetry_keys = (set(teams_map.keys()) | set(today_dds_teams.keys())) - telemetry_keys
    items: list[dict[str, Any]] = []
    for key in non_telemetry_keys:
        safe_key = str(key).strip().upper()
        if not safe_key:
            continue
        team_data = teams_map.get(key) or teams_map.get(safe_key) or {}
        dds_entry = today_dds_teams.get(safe_key)
        if not dds_entry:
            for t_code, entry_val in today_dds_teams.items():
                if _normalized_alias_matches(t_code, safe_key):
                    dds_entry = entry_val
                    break
        completed_at_str = None
        if isinstance(dds_entry, dict):
            completed_at_str = dds_entry.get('completedAt')
        has_dds_today = bool(completed_at_str or dds_entry is not None)
        has_recent_dds, recent_dds_entry = _has_dds_in_recent_map(safe_key, recent_7d_dds)
        recent_completed_at = recent_dds_entry.get('completedAt') if isinstance(recent_dds_entry, dict) else None
        if has_dds_today:
            team_active = True
            turno_inicio = completed_at_str
            dt_ini = to_utc_dt(turno_inicio) if turno_inicio else None
            dds_after_18 = False
            if dt_ini:
                dt_ini_local = dt_ini.astimezone(ZoneInfo(DDS_TIMEZONE)) if DDS_TIMEZONE else dt_ini
                if dt_ini_local.hour >= 18:
                    dds_after_18 = True
            if is_after_18 or dds_after_18:
                estado = 'FECHADO'
                turn_status = 'FECHADO'
                motivo = 'Fechamento automático (18:00)'
                turno_fim = completed_at_str if dds_after_18 else f'{today_iso}T18:00:00-03:00'
                dt_fim = to_utc_dt(turno_fim)
            else:
                estado = 'ABERTO'
                turn_status = 'ABERTO'
                motivo = 'Turno aberto via DDS'
                turno_fim = None
                dt_fim = None
        elif has_recent_dds:
            team_active = True
            estado = 'FECHADO'
            turn_status = 'FECHADO'
            motivo = 'Sem DDS hoje'
            turno_inicio = None
            turno_fim = None
            dt_ini = None
            dt_fim = None
        else:
            team_active = False
            estado = 'FECHADO'
            turn_status = 'FECHADO'
            motivo = '-'
            turno_inicio = None
            turno_fim = None
            dt_ini = None
            dt_fim = None
        last_contact_str = completed_at_str or recent_completed_at or None
        updated_at = last_contact_str or now.isoformat()
        up_dt = to_utc_dt(updated_at)
        minutos = max(0, int((now - up_dt).total_seconds() // 60)) if up_dt else None
        participantes = team_data.get('members') or []
        if isinstance(participantes, str):
            participantes = [p.strip() for p in participantes.split(',') if p.strip()]
        colaborador = participantes[0] if participantes else None
        equipe_nome = team_data.get('displayName') or safe_key
        tablet = team_data.get('tablet') or team_data.get('identificadorEquipamento') or safe_key
        veiculo = team_data.get('veiculo') or None
        dds_presence = get_team_dds_presence(safe_key, available_days, projections_by_day, today_iso)
        if dds_presence.get('ddsToday') == 'ok':
            has_dds_today = True
        dds_days = list(dds_presence.get('ddsDays') or [])
        dds_history = list(dds_presence.get('ddsHistory') or [])
        dds_times = dict(dds_presence.get('ddsTimes') or {})
        if has_dds_today:
            dds_today_val = 'ok'
            if today_iso not in dds_days:
                dds_days.append(today_iso)
                dds_history.append('ok')
            elif dds_days and dds_days[-1] == today_iso and dds_history:
                dds_history[-1] = 'ok'
            if today_iso not in dds_times and dt_ini and DDS_TIMEZONE:
                dds_times[today_iso] = dt_ini.astimezone(ZoneInfo(DDS_TIMEZONE)).strftime('%H:%M')
        else:
            dds_today_val = 'ok' if dds_presence.get('ddsToday') == 'ok' else 'neutral'
        item = {'teamKey': safe_key, 'equipe': equipe_nome, 'veiculo': veiculo, 'setor': team_data.get('setor') or 'TODOS', 'estado': estado, 'estadoOriginal': estado, 'origemAtualizacao': 'TORRE_DDS', 'deviceIdLastWriter': 'TORRE_DDS', 'rotalogSnapshot': None, 'tablet': tablet, 'ss': '-', 'motivo': motivo, 'updatedAt': updated_at, 'openedAtClientMs': int(dt_ini.timestamp() * 1000) if dt_ini else None, 'closedAtClientMs': int(dt_fim.timestamp() * 1000) if dt_fim else None, 'turnoInicio': turno_inicio, 'turnoFim': turno_fim, 'turnStatus': turn_status, 'totalConcluidos': 0, 'ssExecutadasCount': 0, 'minutosDesdeAtualizacao': minutos, 'critico': False, 'alerta': None, 'participantes': participantes, 'colaborador': colaborador, 'motorista': colaborador, 'coringas': [], 'active': team_active, 'lastContact': last_contact_str, 'lastContactSource': 'D' if last_contact_str else None, 'isOnline': False, 'statusConexao': 'offline', 'atividadeStatusRotalog': None, 'monitorStatusRotalog': estado, 'operacional': {'fonte': 'TORRE_DDS', 'disponivel': has_dds_today, 'estado': estado, 'atividade': None, 'servicoAtual': None, 'atualizadoEm': completed_at_str or None, 'veiculo': veiculo, 'identificadorEquipamento': tablet, 'colaborador': colaborador, 'totalConcluidos': 0, 'latitude': None, 'longitude': None}, 'ddsHistory': dds_history, 'ddsToday': dds_today_val, 'ddsDays': dds_days, 'ddsTimes': dds_times, 'ddsPhotos': {}, 'unreadMessages': 0, 'unreadMap': {}, 'lastWasDescansoSemanal': False, 'teamType': team_data.get('teamType')}
        items.append(item)
    return items

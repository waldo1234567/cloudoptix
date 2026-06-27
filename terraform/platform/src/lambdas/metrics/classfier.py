import logging
from enum import Enum
from typing import Dict, Any

logger = logging.getLogger()


class WorkloadPattern(str, Enum):
    ALWAYS_ON_ACTIVE = "ALWAYS_ON_ACTIVE"
    ALWAYS_ON_IDLE = "ALWAYS_ON_IDLE"
    SPIKY = "SPIKY"
    SCHEDULED = "SCHEDULED"
    DECLINING = "DECLINING"
    ABANDONED = "ABANDONED"
    UNKNOWN = "UNKNOWN"
    

def _classify_compute_pattern(cpu_avg:float, cpu_p99:float, is_unsafe: bool, network_active: bool) -> WorkloadPattern:
    """
    Mathematical heuristics for compute (EC2, RDS, ECS) classification based on the 30-day 
    relationship between Average and P99 utilization.
    """
    
    if not is_unsafe and cpu_p99 < 1.0 and not network_active:
        return WorkloadPattern.ABANDONED
    
    if cpu_p99 < 5.0 and cpu_avg < 2.0:
        return WorkloadPattern.ALWAYS_ON_IDLE
    
    if cpu_p99 > 40.0 and cpu_avg < 10.0:
        return WorkloadPattern.SPIKY
    
    if cpu_p99 > 20.0 and (cpu_p99 * 0.15) <= cpu_avg <= (cpu_p99 * 0.45):
        return WorkloadPattern.SCHEDULED
    
    if cpu_avg > (cpu_p99 * 0.45):
        return WorkloadPattern.ALWAYS_ON_ACTIVE

    return WorkloadPattern.UNKNOWN


def classify_resource(resource_data: Dict[str, Any])  -> WorkloadPattern:
    """
    Evaluates 30-day metric statistics to classify the architectural workload pattern.
    """
    res_type = resource_data.get('ResourceType')
    metrics = resource_data.get('MetricSnapshot', {})
    is_unsafe = resource_data.get('IsUnsafe', True)
    
    if not metrics:
        return WorkloadPattern.UNKNOWN
    
    if res_type in ['instance', 'db-instance', 'service']:
        cpu_avg = metrics.get('CPUUtilization', {}).get('Average', 0.0)
        cpu_p99 = metrics.get('CPUUtilization', {}).get('p99', 0.0)
        network_active = False
        
        if res_type == 'instance' :
            net_in = metrics.get('NetworkIn', {}).get('Average', 0.0)
            net_out = metrics.get('NetworkOut', {}).get('Average', 0.0)
            network_active = (net_in > 100000) or (net_out > 100000)
        elif res_type == 'db-instance':
            connections = metrics.get('DatabaseConnections', {}).get('Maximum', 0)
            network_active = connections > 0
        elif res_type == 'service':
            network_active = True
        
        return _classify_compute_pattern(cpu_avg, cpu_p99, is_unsafe, network_active)
    
    
    if res_type == 'volume':
        state = resource_data.get('RawMetadata', {}).get('State')
        if state == 'available':
            return WorkloadPattern.ABANDONED

    if res_type == 'eipalloc':
        return WorkloadPattern.UNKNOWN

    if res_type == 'bucket':
        return WorkloadPattern.UNKNOWN

    if not metrics:
        return WorkloadPattern.UNKNOWN
    
    if res_type == 'volume':
        read_ops = metrics.get('VolumeReadOps', {}).get('Maximum', 0)
        write_ops = metrics.get('VolumeWriteOps', {}).get('Maximum', 0)
        idle_time = metrics.get('VolumeIdleTime', {}).get('Average', 0.0)
        
        if read_ops == 0 and write_ops == 0 and idle_time > 99.0:
            return WorkloadPattern.ABANDONED if not is_unsafe else WorkloadPattern.ALWAYS_ON_IDLE
        return WorkloadPattern.ALWAYS_ON_ACTIVE
    
    if res_type == 'function':
        invocations = metrics.get('Invocations', {}).get('Sum', 0)
        throttles = metrics.get('Throttles', {}).get('Maximum', 0)
        
        if invocations == 0 and throttles == 0:
            return WorkloadPattern.ABANDONED
        if invocations < 100:
            return WorkloadPattern.ALWAYS_ON_IDLE
    
        return WorkloadPattern.SPIKY
    
    if res_type == 'loadbalancer':
        requests_max = metrics.get('RequestCount', {}).get('Maximum', 0)
        requests_avg = metrics.get('RequestCount', {}).get('Average', 0)
        
        if requests_max == 0:
            return WorkloadPattern.ABANDONED
        
        if requests_max > (requests_avg * 10) and requests_avg > 0:
            return WorkloadPattern.SPIKY
        
        return WorkloadPattern.ALWAYS_ON_ACTIVE

    
    if res_type == 'natgateway':
        bytes_out = metrics.get('BytesOutToDestination', {}).get('Average', 0)
        connections = metrics.get('ActiveConnectionCount', {}).get('Maximum', 0)

        if bytes_out < 1000 and connections == 0:
            return WorkloadPattern.ABANDONED
        
        return WorkloadPattern.ALWAYS_ON_ACTIVE
    
    
    if res_type == 'table':
        read_p99 = metrics.get('ConsumedReadCapacityUnits', {}).get('p99', 0)
        read_avg = metrics.get('ConsumedReadCapacityUnits', {}).get('Average', 0)
        write_p99 = metrics.get('ConsumedWriteCapacityUnits', {}).get('p99', 0)
        
        if read_p99 == 0 and write_p99 == 0:
            return WorkloadPattern.ABANDONED
        
        if (read_p99 > read_avg * 5) and (write_p99 > read_avg * 5):
            return WorkloadPattern.SPIKY
        
        return WorkloadPattern.ALWAYS_ON_ACTIVE
    
    if res_type == 'cluster':
        curr_connections = metrics.get('CurrConnections', {}).get('Maximum', 0)
        cpu_avg = metrics.get('CPUUtilization', {}).get('Average', 0.0)
        
        if curr_connections == 0:
            return WorkloadPattern.ABANDONED if not is_unsafe else WorkloadPattern.ALWAYS_ON_IDLE
        if cpu_avg < 2.0:
            return WorkloadPattern.ALWAYS_ON_IDLE
        return WorkloadPattern.ALWAYS_ON_ACTIVE
    
    if res_type == 'filesystem':
        client_connections = metrics.get('ClientConnections', {}).get('Maximum', 0)
        
        if client_connections == 0:
            return WorkloadPattern.ABANDONED if not is_unsafe else WorkloadPattern.ALWAYS_ON_IDLE
            
        return WorkloadPattern.ALWAYS_ON_ACTIVE
    
    return WorkloadPattern.ALWAYS_ON_ACTIVE

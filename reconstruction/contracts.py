"""Discoverable MCP operation input contracts."""
def schema(operation):
    definitions={
        'crop_cloud':(['source','bounds_min','bounds_max'],{'source':'string','bounds_min':'array','bounds_max':'array'}),
        'symmetry_cloud':(['source','center','angle_degrees','min_height','voxel_size'],{'source':'string','center':'array','angle_degrees':'number','min_height':'number','voxel_size':'number'}),
        'preprocess_photos':(['entries'],{'entries':'array'}),
        'attempt_colmap':(['images'],{'images':'string','masks':'string'}),
        'build_geometry':([] ,{'spec':'object','spec_path':'string'}),
        'import_voxels':(['source'],{'source':'string'}),
        'map_materials':(['geometry','cloud','transform'],{'geometry':'string','cloud':'string','transform':'array','palette':'object','palette_path':'string','policy':'object'}),
        'validate_build':(['geometry'],{'geometry':'string'}),
        'export_schem':(['geometry','data_version'],{'geometry':'string','palette':'object','palette_path':'string','data_version':'integer'}),
        'preview_build':(['geometry'],{'geometry':'string','palette':'object','palette_path':'string'}),
        'run_pipeline':(['data_version'],{'spec':'object','spec_path':'string','geometry_source':'string','palette':'object','palette_path':'string','cloud':'string','transform':'array','policy':'object','data_version':'integer'})}
    if operation not in definitions: raise ValueError('unknown operation')
    required,props=definitions[operation]
    properties={k:{'type':v} for k,v in props.items()}
    if 'palette' in properties: properties['palette']['type']=['object','array']
    if 'data_version' in properties: properties['data_version']['minimum']=1
    result={'type':'object','required':required,'properties':properties,'additionalProperties':False}
    groups=[]
    if operation=='build_geometry': groups.append(['spec','spec_path'])
    if operation=='run_pipeline': groups.append(['spec','spec_path','geometry_source'])
    if 'palette' in properties: groups.append(['palette','palette_path'])
    if groups: result['allOf']=[{'oneOf':[{'required':[field]} for field in group]} for group in groups]
    return result

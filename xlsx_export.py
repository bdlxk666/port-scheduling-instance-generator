"""Portable XLSX export for users running the generator without third-party packages.

Writes the single, filterable Tasks sheet. Current task start times remain blank.
"""
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree.ElementTree import Element, SubElement, tostring

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'

def column(n):
    out=''
    while n:
        n,r=divmod(n-1,26);out=chr(65+r)+out
    return out

def xml(root):
    return tostring(root,encoding='utf-8',xml_declaration=True)

def export_xlsx(tables,path):
    content=Element('Types',xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    for ext,typ in [('rels','application/vnd.openxmlformats-package.relationships+xml'),('xml','application/xml')]:
        SubElement(content,'Default',Extension=ext,ContentType=typ)
    for part,typ in [('/xl/workbook.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'),('/xl/styles.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')]:
        SubElement(content,'Override',PartName=part,ContentType=typ)
    rootrels=Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    SubElement(rootrels,'Relationship',Id='rId1',Type=REL+'/officeDocument',Target='xl/workbook.xml')
    wb=Element('workbook',xmlns=NS,attrib={'xmlns:r':REL});sheets=SubElement(wb,'sheets')
    rels=Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    SubElement(rels,'Relationship',Id='rIdStyles',Type=REL+'/styles',Target='styles.xml')
    styles=Element('styleSheet',xmlns=NS)
    fonts=SubElement(styles,'fonts',count='2')
    for bold in (False,True):
        font=SubElement(fonts,'font');SubElement(font,'sz',val='11');SubElement(font,'name',val='Arial')
        if bold:SubElement(font,'b');SubElement(font,'color',rgb='FFFFFFFF')
    fills=SubElement(styles,'fills',count='3')
    SubElement(SubElement(fills,'fill'),'patternFill',patternType='none')
    SubElement(SubElement(fills,'fill'),'patternFill',patternType='gray125')
    solid=SubElement(SubElement(fills,'fill'),'patternFill',patternType='solid');SubElement(solid,'fgColor',rgb='FF243B53')
    borders=SubElement(styles,'borders',count='1');SubElement(borders,'border')
    base=SubElement(styles,'cellStyleXfs',count='1');SubElement(base,'xf',numFmtId='0',fontId='0',fillId='0',borderId='0')
    xfs=SubElement(styles,'cellXfs',count='2')
    for i in (0,1):
        xf=SubElement(xfs,'xf',numFmtId='0',fontId=str(i),fillId='2' if i else '0',borderId='0',xfId='0',applyAlignment='1')
        SubElement(xf,'alignment',vertical='center',wrapText='1')
    names=SubElement(styles,'cellStyles',count='1');SubElement(names,'cellStyle',name='Normal',xfId='0',builtinId='0')
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for idx,(name,records) in enumerate(tables.items(),1):
            headers=list(records[0]);matrix=[headers]+[[r[h] for h in headers] for r in records]
            SubElement(sheets,'sheet',name=name,sheetId=str(idx),attrib={'r:id':f'rId{idx}'})
            SubElement(rels,'Relationship',Id=f'rId{idx}',Type=REL+'/worksheet',Target=f'worksheets/sheet{idx}.xml')
            SubElement(content,'Override',PartName=f'/xl/worksheets/sheet{idx}.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            ws=Element('worksheet',xmlns=NS)
            end=f'{column(len(headers))}{len(matrix)}';SubElement(ws,'dimension',ref=f'A1:{end}')
            views=SubElement(ws,'sheetViews');view=SubElement(views,'sheetView',workbookViewId='0',showGridLines='0')
            SubElement(view,'pane',ySplit='1',topLeftCell='A2',activePane='bottomLeft',state='frozen')
            SubElement(ws,'sheetFormatPr',defaultRowHeight='25')
            cols=SubElement(ws,'cols')
            for j,h in enumerate(headers,1):
                width=70 if h in ('value','inbound_route','outbound_route','route','source') else min(30,max(16,len(h)+2))
                SubElement(cols,'col',min=str(j),max=str(j),width=str(width),customWidth='1')
            data=SubElement(ws,'sheetData')
            for i,row in enumerate(matrix,1):
                maxlen=max(len(str(v)) for v in row)
                height=40 if i==1 else max(25,min(110,16*((maxlen//65)+1)))
                rr=SubElement(data,'row',r=str(i),ht=str(height),customHeight='1')
                for j,v in enumerate(row,1):
                    if v is None:continue
                    cc=SubElement(rr,'c',r=f'{column(j)}{i}',s='1' if i==1 else '0')
                    if isinstance(v,bool):cc.set('t','b');SubElement(cc,'v').text='1' if v else '0'
                    elif isinstance(v,(int,float)):SubElement(cc,'v').text=str(v)
                    else:
                        cc.set('t','inlineStr');SubElement(SubElement(cc,'is'),'t').text=str(v)
            SubElement(ws,'autoFilter',ref=f'A1:{end}')
            z.writestr(f'xl/worksheets/sheet{idx}.xml',xml(ws))
        z.writestr('[Content_Types].xml',xml(content));z.writestr('_rels/.rels',xml(rootrels))
        z.writestr('xl/workbook.xml',xml(wb));z.writestr('xl/_rels/workbook.xml.rels',xml(rels));z.writestr('xl/styles.xml',xml(styles))

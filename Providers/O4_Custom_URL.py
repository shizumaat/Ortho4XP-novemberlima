import requests
import time
user_agent_generic="Mozilla/5.0 (X11; Linux x86_64; rv:52.0) Gecko/20100101 Firefox/52.0"
# In case of providers whose request do not follow a regular pattern, you can use code here to form it


############################################################################################################
# list of affected provider_codes
custom_url_list=('Here',)
custom_url_list = custom_url_list+tuple([x + '_NAIP' for x in (
     'AL','AR','AZ','CA','CO','CT','DE','FL','GA','IA','ID','IL',
     'IN','KS','KY','LA','MA','MD','ME','MI','MN','MO','MS','MT',
     'NC','ND','NE','NH','NJ','NM','NV','NY','OH','OK','OR','PA',
     'RI','SC','SD','TN','TX','UT','VA','VT','WA','WI','WV','WY')])
############################################################################################################


############################################################################################################
# might get some session tokens here
############################################################################################################

# Here
Here_time=time.time()
Here_value=None
def get_Here_value():
    global Here_time, Here_value
    while Here_value=="loading":
        print("    Waiting for Here value to be updated.")
        time.sleep(3)
    if (not Here_value) or (time.time()-Here_time)>=10000:
        Here_value="loading"
        # The "loading" sentinel must never survive a failed scrape: other
        # download threads spin on it forever (this exact hang killed the
        # old Norway NIB provider). On any error, reset and re-raise.
        try:
            js_path = str(requests.get('https://wego.here.com', timeout=15).content).split('script type="module" crossorigin src="')[1].split('"')[0]
            print("js_path: "+js_path)
            Here_value=str(requests.get('https://wego.here.com'+js_path, timeout=15).content).split('PLATFORM_API_KEY:"')[1][:100].split('"')[0]
            print("Here_value: "+Here_value)
            Here_time=time.time()
        except Exception:
            Here_value=None
            raise
    return Here_value

############################################################################################################

def custom_wms_request(bbox,width,height,provider):
    if '_NAIP' in provider['code']:
        (xmin,ymax,xmax,ymin)=bbox
        url="https://gis.apfo.usda.gov/arcgis/rest/services/NAIP_Historical/"+provider['code']+"/ImageServer/exportImage?f=image&bbox="+str(xmin)+"%2C"+str(ymin)+"%2C"+str(xmax)+"%2C"+str(ymax)+"&imageSR=102100&bboxSR=102100&size="+str(width)+"%2C"+str(height)
        return (url,None)

def custom_tms_request(tilematrix,til_x,til_y,provider):
    if provider['code']=='Here':
        Here_value=get_Here_value()
        fake_headers={'User-Agent':user_agent_generic,'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8','Connection':'keep-alive','Accept-Encoding':'gzip, deflate','Referer':'https://wego.here.com/'}
        url="https://maps.hereapi.com/v3/background/mc/"+str(tilematrix)+"/"+str(til_x)+"/"+str(til_y)+"/jpeg?apikey="+Here_value+"&style=satellite.day&ppi=100&size=256"
        return (url,fake_headers)

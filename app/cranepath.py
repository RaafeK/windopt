import json
import os
import flask
from flask import request, jsonify, g, send_file
from app import app, celery
from werkzeug.utils import secure_filename
from windscripts.tsp import *
import fiona
import pandas as pd
from app.dbmodel import *
import auth
from upload import allowed_file, ZIP
from zipfile import ZipFile
import shutil
from mongoengine import *
from mongoengine.dereference import DeReference
from cStringIO import StringIO
from errors import ProjectException
import uuid
#NOTES:
#This implementation requires heavy use of a file system which in turns has all
#the nuances of permission management. Thus, in a more refined version, it would
#be obviously necessary to use a DB system instead of a local file system.
#Also, currently most GIS operations are handled by OGR/GDAL bindings. A proper
#solution would take advantage of Rasterio/Shapely/Fiona to avoid having to
#make system calls and obfuscated code. Another reason, OGR/GDAL doesn't give
#warning when there is a failure to create a new file, instead it just returns
#a NoneType

SHP_DIR = app.config['UPLOAD_FOLDER']

def clear_uploads(DIR):
    shpdir = DIR
    if os.path.exists(shpdir):
        for the_file in os.listdir(shpdir):
            file_path = os.path.join(shpdir, the_file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception, e:
                print e

@app.route('/api/cranepath/<project_name>/status',methods=['GET'])
@auth.login_required
def get_crane_status(project_name):
    user, project = Project.get_user_and_project(g.username, project_name)
    if hasattr(project.crane_project, "status") and project.crane_project.status == "Solved.":
        result = {}
        result['schedule'] = project.crane_project.geojson
        result['features'] = [{"name":feature.name, "geojson":feature.geojson} for feature in project.crane_project.features]
        result['turbines'] = project.crane_project.turbines.geojson
        result['boundary'] = project.crane_project.boundary.geojson
        result['status'] = project.crane_project.status
        return jsonify(result = result)
    elif hasattr(project.crane_project, "status") and project.crane_project.status == "Shapefiles stored. User needs to enter Interpretations":
        result = {}
        layers = [feature.name for feature in project.crane_project.features]
        print layers
        if project.crane_project.turbines:
            layers.append(project.crane_project.turbines.name)
        if project.crane_project.boundary:
            layers.append(project.crane_project.boundary.name)
        result["layers"] = layers
        result['status'] = project.crane_project.status
        return jsonify(result = result)
    elif hasattr(project.crane_project, "status"):
        return jsonify(
            result = {"status": project.crane_project.status}
        )
    else:
        raise ProjectException("Server Side Error")

@app.route('/api/cranepath/<project_name>/zipupload',methods=['POST'])
@auth.login_required
def upload_ziplfile(project_name):
    print request.form
    print "At upload"
    print globals()
    if request.method == 'POST':
        fileobj = request.files['file']
        print "Got File"
        if fileobj and allowed_file(fileobj.filename,ZIP):
            print "File is allowed"
            user, project = Project.get_user_and_project(g.username, project_name)
            #TODO: Store file in DB, set status as got file and unpacking layers.
            try:
                if hasattr(project.crane_project, "zipfile"):
                    project.crane_project.zipfile.delete()
                    project.crane_project.delete()
                    newCrane = CraneProject()
                    newCrane.save()
                    print newCrane.status
                    project.crane_project = newCrane
                    print project.crane_project.status
                    project.save()
                project.crane_project.zipfile.put(fileobj , content_type='application/zip')
                project.crane_project.status = "Project zip file stored, queueing for unpacking layers"
                project.crane_project.save()
                unpack_layers.delay(g.username, project_name)
            except Exception as e:
                project.crane_project.status = "Error storing zip file."
                project.save(cascade = True)
                raise ProjectException("Error storing zip file")
            return flask.jsonify(result={"message": "Project zip file stored, queued for unpacking layers"})
        else:
            raise ProjectException("Geographic files should be packed in a ZIP file")

#--- Unpacking function ---
@celery.task(name = 'unpack_layers')
def unpack_layers (username, project_name):
    try:
        #TODO: This still uses the file system
        user, project = Project.get_user_and_project(username, project_name)
        clear_uploads(SHP_DIR)
        zip_contents = StringIO(project.crane_project.zipfile.read())
        unique_name = str(uuid.uuid1())
        zipfile = SHP_DIR + '/' + unique_name +'.zip'
        with open(zipfile, "wb") as f:
            f.write(zip_contents.getvalue())
        #TODO: keep track of the data types.
        print " AT FIONA DRIVERS"
        messages = []
        project.crane_project.status = "Reading shapefiles."
        project.save(cascade = True)
        print globals()
        with fiona.drivers():
            for i, layername in enumerate(
                fiona.listlayers(
                '/',
                vfs='zip://'+zipfile)):
                feature = GeoFeat()
                feature.read_shapefile(layername, zipfile)
                feature.name = layername
                #TODO: This just leaves shitty layers out of the project, you need to report this.
                try:
                    feature.save()
                    project.crane_project.features.append(feature)
                except Exception as e:
                    messages.append(layername + ' not saved, reason: '+ str(e))
                    continue
                    #TODO: These two calls might be redundant, check if its so.
        project.crane_project.status = "Shapefiles stored. User needs to enter Interpretations"
        print messages
        project.save(cascade = True)
        return "Layers stored"
    except Exception as e:
        project.crane_project.status = "Error unpacking layers"
        project.crane_project.messages = "Error unpacking layers: " + str(e)
        project.save(cascade = True)
        return

@app.route('/api/cranepath/<project_name>/tsp',methods=['POST'])
@auth.login_required
def schedule_tsp(project_name):
    user, project = Project.get_user_and_project(g.username, project_name)
    try:
        layerdict = request.json['layerdict']
    except Exception as e:
        raise ProjectException("Error: " + str(e))
    calculate_tsp.delay(g.username, project_name, layerdict)
    project.crane_project.status = "Optimization problem queued"
    project.save(cascade = True)
    return flask.jsonify(message = "Optimization problem queued")

@celery.task(name = "calculate_tsp")
def calculate_tsp(username, project_name, layerdict):
    #Create Layer Dictionary and identify turbines
    print "we are at tsp_sol()"
    try:
        #Get the project
        user, project = Project.get_user_and_project(username, project_name)
        project.crane_project.geojson = {}
        project.crane_project.csv_schedule.delete()
        project.crane_project.status = "Setting layer interpretations."
        project.crane_project.save()

        #TODO: Interpretations that don't match the existing location of the feature should change the location of the feature
        #TODO: example, a turbine misinterpreted originally as a feature should be  siwtched back to turbine
        #TODO: A layer assigned as a boundary, should be switched back to feature.
        messages = []
        for feature in project.crane_project.features:
            feature.cost = float(layerdict[feature.name]['cost'])
            feature.interpretation = layerdict[feature.name]['interpretation']
            try:
                feature.save()
            except:
                print layer + ': not saved'
                messages.append(layer + ': not saved')
                continue
            if feature.interpretation == 'turbines':
                project.crane_project.turbines = feature
                'we got turbines'
            elif feature.interpretation == 'boundary':
                project.crane_project.set_boundary(feature)
                print 'we got a boundary'
            else:
                project.crane_project.features.append(feature)

        project.crane_project.messages = ";".join(messages)
        project.crane_project.status = "Layer Interpretations set"
        project.crane_project.save()

        #Create Cost Ratser
        print "cost raster"
        project.crane_project.status = "Creating the cost raster."
        project.crane_project.save()
        project.crane_project.createCostRaster()

        #Create Complete NetworkX graph
        print 'graph'
        project.crane_project.status = "Building the complete graph."
        project.crane_project.save()
        project.crane_project.create_nx_graph()

        #Solve the graph
        print 'tsp'
        project.crane_project.status = "Solving the Traveling Salesman Problem"
        project.crane_project.save()
        project.crane_project.solve_tsp()
        project.crane_project.status = "Getting detailed path costs."
        project.crane_project.save()
        project.crane_project.expandPaths()

        #Save to GeoJSON
        print 'GeoJson'

        schedule = project.crane_project.get_geojson()
        project.crane_project.geojson = schedule
        project.crane_project.status = "Solved."
        project.save(cascade = True)

        #Save to CSV
        print 'csv'
        activities = []
        for part in schedule['features']: activities.append(part['properties'])
        print activities[:5]
        strio = StringIO()
        pd.DataFrame(activities).to_csv(strio)
        strio.seek(0)
        project.crane_project.csv_schedule.put(strio, content_type='text/csv')
        project.crane_project.status = "Solved."
        project.save(cascade = True)
        return "Crane Path Solved"

    except Exception as e:
        project.crane_project.status = "Error computing TSP"
        project.crane_project.messages = "Error computing TSP: " + str(e)
        project.save(cascade = True)
        return


@app.route('/api/cranepath/<project_name>/schedule.csv',methods=['GET'])
#TODO: @auth.login_required
def csv(project_name):
    #TODO: user, project = Project.get_user_and_project(g.username, project_name)
    project = Project.objects.get(name = project_name)
    return send_file(project.crane_project.csv_schedule,
        attachment_filename= project_name + ".csv",
        as_attachment=True)

### This script creates clones of grafana dashboards that uses influxdb as data source; 
###   with the periodicity word in dashboard name like *Hourly, *Daily, *Weekly
###   to visualize the data summarized with reduced sampling intervals.
### Run this script from crontab on a server where grafana is installed on a daily basis
###   so that any updates made to real time visualization dashboards are also available for the
###   the dashboards that show summary data for a longer period.
### This script can also create the influxdb bucket names with matching symmary words like Hourly, Daily
### If SummaryGroups and SummaryValueTypes are not defined in config file,
###   this script assumes that the continuous query tasks are defined in influxdb to summarize the data
###   on a hourly and on a daily basis. 
###   Assumed bucket names 
###     for min value for each hour     - <realTimeBucket>HourlyMin
###     for max value for each hour     - <realTimeBucket>HourlyMax
###     for mean value for each hour    - <realTimeBucket>HourlyMean
###     for min value for a day         - <realTimeBucket>DailyMin
###     for max value for a day         - <realTimeBucket>DailyMax
###     for mean value for a day        - <realTimeBucket>DailyMean
### Else, the bucket names are derived from given SummaryGroups and SummaryValueTypes
### This script creates new dashboads, changes the influxdb query within each panel by replacing the
###  <realTimeBucket> name with  group name like *Hourly*,  *Daily* names defined in config file
###    for each type of dashboards.
### For more details on how all these tie together, refer TBD 
### 2024-07-16 havembha@gmail.com 01.00.00
import requests
import json
import re
from urllib3.exceptions import InsecureRequestWarning
from urllib3 import disable_warnings
from collections import defaultdict
import argparse
import signal
import sys
import os
import platform
import time
import copy
import datetime

### incluxdb 2.0 client library
from influxdb_client import InfluxDBClient, TasksService, TaskCreateRequest
from influxdb_client.rest import ApiException

disable_warnings(InsecureRequestWarning)

### initial version
JAVersion = "01.00.00"

### debug level 0 to 4, 4 for highest info
### parameter passed from command line supercedes the value defined in config file and below setting
debugLevel = 0

# get current hostname, this is used to find the current environment and pick up environment specific parameters from config file
thisHostName = platform.node()
# if hostname has domain name, strip it
hostNameParts = thisHostName.split('.')
thisHostName = hostNameParts[0]

startTimeInSec = time.time() 

### default log file name, superceded by the definition in config file
logFileName = "JACloneGrafanaDashboards.log"

### values of config parameters are stored with config param name as key
### keys [ ''ConfigFile', 'GrafanaAPIURL', 'GrafanaAPIKey', 'SummaryGroups', 'SummaryValueTypes' ]
JAConfigParams = defaultdict(dict)
JAStats = {'INFO':0, 'WARN':0, 'ERROR': 0, 'TotalDashboards':0,'NonSummaryDashboards':0,'OldSummaryDashboards':0, 'NewSummaryDashboards':0, 'LibraryPanels':0,'SummaryLibraryPanels':0, 'SummaryPanels':0, 'SummaryTargets':0, 'SummaryTemplates':0, 'SkippedSummaryDashboards':0, 'NoChangeToSummaryDashboard':0}

### this hash contains the details of existing summary dashboards
### this is used to update existing summary dashboard with latest details
summaryDashboardsHash = defaultdict(dict) 

### combine groups and valueTypes to formulate regex search expression to match the
###  summary dashboard name (to skip those while creating new summary dashboard from
###   non-summary dashboard)
### regex format: r'[[<groups>][<valueTypes>]
regexSearchForDashboardTitle = None

def JALogMsg(message:str, fileName:str, appendDate=True, prefixTimeStamp=True):
    """"
    Logs the given message to a log file in append mode, 
      if appendDate is True, file name ending with YYYYMMDD is assumed.
      If prefixTimeStamp is True, current dateTime string is prefixed to the log line before logging
    """
    if fileName == None:
        print(message)
        return 0
        
    if appendDate == True:
        logFileName = "{0}.{1}".format( fileName, datetime.datetime.now().strftime("%Y%m%d") )
    else:
        logFileName = fileName

    try:
        logFileStream = open( logFileName, 'a')
    except OSError:
        return 0
    else:
        if ( prefixTimeStamp == True) :
            logFileStream.write( datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f%Z") + " " + message + "\n" )
        else:
            logFileStream.write(message + "\n" )
        logFileStream.close()
        return 1

def JAPrepareFluxQueriesForTasks( summaryGroups,                                 
                                summaryBucketDetails,
                                org ):
    """
    This function prepares the continuous queries to summarize data for the summary bucket names in summaryBucketDetails 
        covering all measurements of the parent bucket.
    Parameters passed:
        summaryGroups - list like ['Hourly', 'Daily', 'Weekly'] 
        summaryBucketDetails - dictionary with bucket name as key and as value array - measurements, associated parent bucket name and
                        flag indicating to create query
        org - organization under which the bucket is present

    If 'SummaryGroups' has 'Hourly', prepare query to summarize bucket per hour
        from(bucket: "<bucketName>")
        |> range(start: -task.every)
        |> filter(fn: (r) => r._measurement == "<measurement>")
        |> aggregateWindow(every: 1h, fn: (mean|min|max based on <valueType>), createEmpty: true)
        |> to(bucket: "<bucketName>Hourly<valueType>", org: "<org>")
        
    If 'SummaryGroups' has 'Daily', prepare query to summarize bucket per day
        from(bucket: "<bucketName>Hourly<ValueType>")
        |> range(start: -task.every)
        |> filter(fn: (r) => r._measurement == "<measurement>")
        |> aggregateWindow(every: 1d, fn: <mean|min|max based on <valueType>>, createEmpty: true)
        |> to(bucket: "<bucketName>Daily<valueType>", org: "<org>")

    If 'SummaryGroups' has 'Weekly', prepare query to summarize bucket per week
        from(bucket: "<bucketName>Daily<ValueType>")
        |> range(start: -task.every)
        |> filter(fn: (r) => r._measurement == "<measurement>")
        |> aggregateWindow(every: 7d, fn: <mean|min|max based on <valueType>>, createEmpty: true)
        |> to(bucket: "<bucketName>Weekly<valueType>", org: "<org>")
        
    Return value:
        returnStatus - True upon success, False if failed
        queries - in list form
    """
    global debugLevel, logFileName, JAConfigParams

    returnStatus = True

    if ( debugLevel ):
        errorMsg = f"DEBUG-1 JAPrepareFluxQueriesForTasks() summaryGroups:|{summaryGroups}|, summaryBucketDetails:|{summaryBucketDetails}|, org:|{org}|"
        JALogMsg( errorMsg, logFileName, True, True)

    fluxQueries = defaultdict(dict)
    hourlyQuery = dailyQuery = weeklyQuery = ''
    returnHourlyQuery = returnDailyQuery = returnWeeklyQuery = False

    ### prepare continuous query for each bucket name separately
    for summaryBucketName in summaryBucketDetails:
        if( summaryBucketDetails[summaryBucketName]['present'] == False ) :
            continue
        bucketName = summaryBucketDetails[summaryBucketName]['parentBucket']
        summaryGroup = summaryBucketDetails[summaryBucketName]['summaryGroup']
        valueType = summaryBucketDetails[summaryBucketName]['summaryValueType']
        tempFunction = valueType.lower()

        if( summaryGroup == 'Hourly'):
            interval = "1h"
            returnHourlyQuery = True
        elif ( summaryGroup == 'Daily'):
            interval = "1d"
            returnDailyQuery = True
        elif ( summaryGroup == 'Weekly'):
            interval = "7d"
            returnWeeklyQuery = True

        taskName = f"Task-S-{summaryGroup}"
        for measurement in summaryBucketDetails[summaryBucketName]['measurements']:
            if ( summaryGroup == 'Hourly'):
                tempQuery = f"""

from (bucket: "{bucketName}")
|> range(start: -task.every)
|> filter(fn: (r) => r._measurement == "{measurement}")
|> aggregateWindow(every: {interval}, fn: {tempFunction})
|> to (bucket: "{summaryBucketName}", org: "{org}")
"""

                if ( hourlyQuery == '' ):
                    hourlyQuery = f"option task = {{ name: \"{taskName}\", every: {interval} }}"
                hourlyQuery = hourlyQuery + tempQuery

            elif ( summaryGroup == 'Daily'):
                ### if 'Hourly' summary group is present, use that bucket to summarize per day.
                ### else, summarize from parent bucket
                if ( 'Hourly' in summaryGroups ):
                    tempQuery = f"""

from (bucket: "{bucketName}Hourly{valueType}")
|> range(start: -task.every)
|> filter(fn: (r) => r._measurement == "{measurement}")
|> aggregateWindow(every: {interval}, fn: {tempFunction})
|> to (bucket: "{summaryBucketName}", org: "{org}")
"""
                else:
                    tempQuery = f"""

from (bucket: "{bucketName}")
|> range(start: -task.every)
|> filter(fn: (r) => r._measurement == "{measurement}")
|> aggregateWindow(every: {interval}, fn: {tempFunction})
|> to (bucket: "{summaryBucketName}", org: "{org}")
"""
                if ( dailyQuery == '' ):
                    dailyQuery = f"option task = {{ name: \"{taskName}\", every: {interval} }}"
                dailyQuery = dailyQuery + tempQuery

            elif ( summaryGroup == 'Weekly'):
                ### if 'Daily' summary group is present, use that bucket to summarize per week.
                ### else if 'Hourly' summary group is present, use that bucket to summarize per week
                ### else, summarize from parent bucket
                if ( 'Daily' in summaryGroups):
                    tempQuery = f"""

from (bucket: "{bucketName}Daily{valueType}")
|> range(start: -task.every)
|> filter(fn: (r) => r._measurement == "{measurement}")
|> aggregateWindow(every: {interval}, fn: {tempFunction})
|> to (bucket: "{summaryBucketName}", org: "{org}")
"""
                elif( 'Hourly' in summaryGroups):
                    tempQuery = f"""

from (bucket: "{bucketName}Hourly{valueType}")
|> range(start: -task.every)
|> filter(fn: (r) => r._measurement == "{measurement}")
|> aggregateWindow(every: {interval}, fn: {tempFunction})
|> to (bucket: "{summaryBucketName}", org: "{org}")
"""
                else:
                    tempQuery = f"""

from (bucket: "{bucketName}")
|> range(start: -task.every)
|> filter(fn: (r) => r._measurement == "{measurement}")
|> aggregateWindow(every: {interval}, fn: {tempFunction})
|> to (bucket: "{summaryBucketName}", org: "{org}")
"""
                if ( weeklyQuery == '' ):
                    weeklyQuery = f"option task = {{ name: \"{taskName}\", every: {interval} }}"
                weeklyQuery = weeklyQuery + tempQuery
             

            if ( debugLevel > 1 ):
                errorMsg = f"DEBUG-2 JAPrepareFluxQueriesForTasks() summaryBucketName:|{summaryBucketName}|, measurement:|{measurement}|, fluxQuery:|{tempQuery}|, taskName:|{taskName}|"
                JALogMsg( errorMsg, logFileName, True, True)

    if( returnHourlyQuery ):
        fluxQueries['Hourly'] = hourlyQuery
    if ( returnDailyQuery ):
        fluxQueries['Daily'] = dailyQuery
    if ( returnWeeklyQuery ):
        fluxQueries['Weekly'] = weeklyQuery

    return returnStatus, fluxQueries

def JAApplyBucketIncludeExcludeRules( bucketName ):
    """
    If 'IncludeBuckets' is specified, it matches the bucketName passed to the spec.
      If no match, returns False
    If 'ExcludeBuckets' is specified, it matches the bucketName passed to the spec.
      If match, returns False
    Else, returns True
    """
    global JAConfigParams, debugLevel, logFileName

    returnStatus = True

    ### check with include spec
    if ( 'IncludeBuckets' in JAConfigParams):
        if( JAConfigParams['IncludeBuckets'] != 'all' ):
            if( re.search(JAConfigParams['IncludeBuckets'], bucketName) == None ): 
                returnStatus = False
                if( debugLevel ):
                    errorMsg = f"DEBUG-1 JAApplyBucketIncludeExcludeRules() bucketName:|{bucketName}| does not match to 'IncludeBuckets' spec:|{JAConfigParams['IncludeBuckets']}|, skipping this bucket\n"
                    JALogMsg( errorMsg, logFileName, True, True)
    if ( returnStatus ):
        if ( 'ExcludeBuckets' in JAConfigParams):
            if( JAConfigParams['ExcludeBuckets'] != 'none' ):
                if( re.search(JAConfigParams['ExcludeBuckets'], bucketName) != None ): 
                    returnStatus = False 
                    if( debugLevel ):
                        errorMsg = f"DEBUG-1 JAApplyBucketIncludeExcludeRules() bucketName:|{bucketName}| matches to the 'ExcludeBuckets' spec:|{JAConfigParams['ExcludeBuckets']}|, skipping this bucket\n"
                        JALogMsg( errorMsg, logFileName, True, True)

    return returnStatus

def JAGetInfluxdbBucketMeasurements(query_api, bucketName):
    """
    This function returns the measurements within given bucket
    """
    # Define the Flux query to get measurements
    tempQuery = f"""
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{bucketName}")
"""
    measurements = set()
    try:
        # Execute the query
        result = query_api.query(tempQuery)

        for table in result:
            for record in table.records:
                measurements.add(record.get_value())
    except:
        errorMsg = f"ERROR JAGetInfluxdbBucketMeasurements() failed to get measurements of bucket:|{bucketName}|"
        JALogMsg( errorMsg, logFileName, True, True)
        measurements = set()

    if( debugLevel ):
        errorMsg = f"DEBUG-1 JAGetInfluxdbBucketMeasurements() bucket:|{bucketName}|, measurements:|{measurements}|"
        JALogMsg( errorMsg, logFileName, True, True)

    return measurements

def JAManageInfluxdbBuckets():
    """
    This function runs continuous queries to summarize data per the SummaryGroups and SummaryValueType definitions

    Connects to influxdb using URL, and token in config file. Reads all buckets in the given org that does not contain the summary bucket suffix.
    For each bucket and for each measurement, calls JAPrepareFluxQueriesForTasks() to prepare continuous queries and executes those queries.

    Return value:
        returnStatus - True upon success, Fales if failed
    """
    global debugLevel, logFileName, JAConfigParams
    
    returnStatus = True

    errorMsg = ''
    if( 'InfluxdbURL' not in JAConfigParams ):
        errorMsg = f"ERROR JAManageInfluxdbBuckets() Fatal error, config file does not have InfluxdbURL defined\n"
    if( 'InfluxdbToken' not in JAConfigParams):
        errorMsg = errorMsg +  f"ERROR JAManageInfluxdbBuckets() Fatal error, config file does not have InfluxdbToken defined\n"
    if( 'InfluxdbOrg' not in JAConfigParams):
        errorMsg = errorMsg +  f"ERROR JAManageInfluxdbBuckets() Fatal error, config file does not have InfluxdbOrg defined\n"
    if ( errorMsg != '' ):
        JALogMsg(errorMsg, logFileName, True, True)
        return False
    try:
        if( debugLevel > 2 ):
            client = InfluxDBClient(url=JAConfigParams['InfluxdbURL'], token=JAConfigParams['InfluxdbToken'], org=JAConfigParams['InfluxdbOrg'], debug=True)
        else:
            client = InfluxDBClient(url=JAConfigParams['InfluxdbURL'], token=JAConfigParams['InfluxdbToken'], org=JAConfigParams['InfluxdbOrg'], debug=False)
    except:
        errorMsg = f"ERROR JAManageInfluxdbBuckets() failed to create influxdb client"
        JALogMsg(errorMsg, logFileName, True, True)
        returnStatus = False

    ### contains all summary bucket names with asssociated non-summary bucket name as value.
    ###  this is used to track the presence of all needed summary buckets so that any missing summary bucket can be created/managed
    ###  this is to handle any addition of SummaryGroups or SummaryValueTypes to the config file.
    summaryBucketDetails = defaultdict(dict)

    try:
        query_api = client.query_api()
        buckets_api = client.buckets_api()
        buckets = buckets_api.find_buckets()

    except ApiException as err:
        errorMsg = f"ERROR JAManageInfluxdbBuckets() failed to get buckets from influxdb org:|{JAConfigParams['InfluxdbOrg']}|, error:|{err}|"
        JALogMsg(errorMsg, logFileName, True, True)
        return False

    except:
        errorMsg = f"ERROR JAManageInfluxdbBuckets() failed to get buckets from influxdb org:|{JAConfigParams['InfluxdbOrg']}|"
        JALogMsg(errorMsg, logFileName, True, True)
        return False

    try:
        ### first get a list of non-summary bucket names,
        ###  prepare corresponding summary bucket names with already present flag set to False
        for bucket in buckets.buckets:
            if( debugLevel > 2):
                errorMsg = f"DEBUG-3 JAManageInfluxdbBuckets() bucket:|{bucket}|"
                JALogMsg(errorMsg, logFileName, True, True)

            if( bucket.type == 'system'):
                ### skip system type of bucket
                continue

            returnStatus = regexSearchForDashboardTitle.search(bucket.name)
            if( returnStatus != None and returnStatus != 0 ) :
                ### skip summary bucket
                continue

            if ( JAApplyBucketIncludeExcludeRules(bucket.name) == False ):
                ### skip this bucket
                continue

            ### get measurements associated with this non-summary bucket
            measurements = JAGetInfluxdbBucketMeasurements(query_api, bucket.name)
            if( len(measurements) == 0 ):
                errorMsg = f"ERROR JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, bucket:|{bucket.name}|, NO measurement in reference bucket, skipping this bucket\n"
                JALogMsg( errorMsg, logFileName, True, True)
                JAStats['ERROR'] += 1
                continue
            
            ###   and save with summary bucket 
            ### non-summary type of user bucket
            ###  prepare corresponding summary bucket names with already present flag set to False
            for groupName in JAConfigParams['SummaryGroups']:
                if( groupName in JAConfigParams['InfluxdbBucketRetentionRules'] ):
                    retentionPeriod = JAConfigParams['InfluxdbBucketRetentionRules'][groupName]
                else:
                    ### default one year for all unknown summary group
                    retentionPeriod = 365

                for valueType in JAConfigParams['SummaryValueTypes']:
                    tempName = f"{bucket.name}{groupName}{valueType}"
                    summaryBucketDetails[tempName]['present'] = False
                    ### convert retention period from days to seconds
                    summaryBucketDetails[tempName]['retentionPeriod'] =  retentionPeriod * 24 * 3600
                    summaryBucketDetails[tempName]['parentBucket'] = bucket.name
                    summaryBucketDetails[tempName]['summaryGroup'] = groupName
                    summaryBucketDetails[tempName]['summaryValueType'] = valueType
                    summaryBucketDetails[tempName]['measurements'] = copy.deepcopy( measurements)

        ### now, for all summary bucket name that match to previously seen non-summary bucket name,
        ###   set the already present flag to True
        for bucket in buckets.buckets:
            if( bucket.type == 'system'):
                ### skip system type of bucket
                continue

            if ( JAApplyBucketIncludeExcludeRules(bucket.name) == False ):
                ### skip this bucket
                continue

            returnStatus = regexSearchForDashboardTitle.search(bucket.name)
            if( returnStatus != None and returnStatus != 0 ) :

                ### set the alredy present flag
                if ( bucket.name in summaryBucketDetails):
                    summaryBucketDetails[bucket.name]['present'] = True
                else:
                    errorMsg = f"WARN JAManageInfluxdbBuckets() summary bucket name:|{bucket.name}| is not associated with any non-summary bucket\n"
                    JALogMsg(errorMsg, logFileName, True, True)
                    JAStats['WARN'] += 1

    except ApiException as err:
        errorMsg = f"ERROR JAManageInfluxdbBuckets() failed to get buckets from influxdb org:|{JAConfigParams['InfluxdbOrg']}|, error:|{err}|"
        JALogMsg(errorMsg, logFileName, True, True)
        return False

    ### create the summary bucket that is not yet present
    try:
        for bucketName in summaryBucketDetails:
            if ( summaryBucketDetails[bucketName]['present'] == False):
                ### create the summary bucket with all the measurements associated with corresponding non-summary bucket
                retentionRules = [{"everySeconds": summaryBucketDetails[bucketName]['retentionPeriod'], 
                                    "shardGroupDurationSeconds": summaryBucketDetails[bucketName]['retentionPeriod']}] 
                try:
                    returnStatus = buckets_api.create_bucket(None,bucketName, JAConfigParams['InfluxdbOrg'],retentionRules)
                    summaryBucketDetails[bucketName]['present'] == True
                except:
                    errorMsg = f"ERROR JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, failed to create the summary bucket:|{bucketName}|, error:|{err}|"
                    JALogMsg(errorMsg, logFileName, True, True)
                    JAStats['ERROR'] += 1
                    continue
    except ApiException as err:
        errorMsg = f"ERROR JAManageInfluxdbBuckets() error processing summary bucket in influxdb org:|{JAConfigParams['InfluxdbOrg']}|, error:|{err}|"
        JALogMsg( errorMsg, logFileName, True, True)
        returnStatus = False

    returnStatus, fluxQueries = JAPrepareFluxQueriesForTasks( JAConfigParams['SummaryGroups'], 
                                    summaryBucketDetails, 
                                    JAConfigParams['InfluxdbOrg'] )

    if ( returnStatus == True ):
        tasks_api = client.tasks_api()
        try:
            ### find existing tasks
            tasks = tasks_api.find_tasks(org=JAConfigParams['InfluxdbOrg'] )

            ### prepare task name search regex string using summaryGroups
            taskNameSearchString = None
            for summaryGroup in JAConfigParams['SummaryGroups']:
                if (taskNameSearchString == None ):
                    taskNameSearchString = "(" + summaryGroup
                else:
                    taskNameSearchString += ( "|" + summaryGroup )
            taskNameSearchString += ")"
            taskNameSearchString = re.compile( f"-S-{taskNameSearchString}" )

            ### delete existing tasks with task name -S-{summaryGroup}
            for task in tasks:
                if( re.search(taskNameSearchString, task.name) != None ):
                    try:
                        returnStatus = tasks_api.delete_task( task.id)
                        if( debugLevel > 1 ):
                            errorMsg = f"DEBUG-2 JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, result of deleting task:|{task.name}|, returnStatus:|{returnStatus}"
                            JALogMsg(errorMsg, logFileName, True, True)
                    except ApiException as err:
                        errorMsg = f"ERROR JAManageInfluxdbBuckets() failed to delete existing task:|{task.name}| in org:|{JAConfigParams['InfluxdbOrg']}|, error:|{err}|"
                        JALogMsg( errorMsg, logFileName, True, True)

        except ApiException as err:
            errorMsg = f"ERROR JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, summary group:|{summaryGroup}|, API exception while executing the continuous query:|{queryString}|, error:|{err}|"
            JALogMsg( errorMsg, logFileName, True, True)

        except Exception as err:
            errorMsg = f"ERROR JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, summary group:|{summaryGroup}|, exception while executing the continuous query:|{queryString}|, error:|{err}|"
            JALogMsg( errorMsg, logFileName, True, True)

        for summaryGroup in fluxQueries:
            taskName = f"Task-S-{summaryGroup}"
            queryString = fluxQueries[summaryGroup]
            try:
                task_request = TaskCreateRequest(flux=queryString, org=JAConfigParams['InfluxdbOrg'], description=taskName, status="active")
                task = tasks_api.create_task(task_create_request=task_request)
                if( debugLevel > 1 ):
                    errorMsg = f"DEBUG-2 JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, queryString:|{queryString}|, task:|{task}"
                    JALogMsg(errorMsg, logFileName, True, True)

            except ApiException as err:
                errorMsg = f"ERROR JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, summary group:|{summaryGroup}|, API exception while executing the continuous query:|{queryString}|, error:|{err}|"
                JALogMsg( errorMsg, logFileName, True, True)

            except Exception as err:
                errorMsg = f"ERROR JAManageInfluxdbBuckets() org:|{JAConfigParams['InfluxdbOrg']}|, summary group:|{summaryGroup}|, exception while executing the continuous query:|{queryString}|, error:|{err}|"
                JALogMsg( errorMsg, logFileName, True, True)
    return returnStatus

def JAPrepareSearchForDashboardTitle(summaryGroups, summaryValueTypes, debugLevel):
    """
    This function prepares the regex search string to be used to identify the
        dashboard or library panel based on the ending portion of the name or title.
    Parameters passed:
        summaryGroups - list like ['Hourly', 'Daily'] 
        summaryValueTypes - list like [ 'Min', 'Max', 'Mean']
        debugLevel - 0 to 4

    Return value:
        compiled regex string in the form '(Hourly|Daily)(Min|Max|Mean)'
    """
    separator = ''
    for groupName in summaryGroups:
        if separator == '' :
            regexSearch= '(' + groupName 
            separator = '|'
        else:
            regexSearch = regexSearch + separator + groupName
    regexSearch = regexSearch + ')('

    separator = ''
    for valueType in summaryValueTypes:
        if separator == '' :
            regexSearch = regexSearch + valueType 
            separator = '|'
        else:
            regexSearch = regexSearch + separator + valueType
    regexSearch = regexSearch + ')'

    if( debugLevel ) :
        errorMsg = f"DEBUG-1 JAPrepareSearchForDashboardTitle() regexSearch: |{regexSearch}|"
        JALogMsg( errorMsg, logFileName, True, True)

    return re.compile(regexSearch)

def JAPrintJsonElements( data, indent=0):
    """
    This function prints key and value pairs of json data
    It calls itself in recursive manner to decode list or dictionary items within the json data.
    Indent value will be increased for each recursive call so that printed key/value pairs
      will be indented to show the root/child/grandchildren relationship

    Return return - nothing is returned
    """
    try:
        if isinstance(data, dict):
            for key, value in data.items():
                errorMsg = ('  ' * indent + str(key) + ':')
                JALogMsg( errorMsg, logFileName, True, True)
                JAPrintJsonElements(value, indent + 1)
        elif isinstance(data, list):
            for index, value in enumerate(data):
                errorMsg = ('  ' * indent + f'[{index}]:')
                JALogMsg( errorMsg, logFileName, True, True)
                JAPrintJsonElements(value, indent + 1)
        else:
            errorMsg = ('  ' * indent + str(data))
            JALogMsg( errorMsg, logFileName, True, True)
    except:
        errorMsg = f"ERROR JAPrintJsonElements() error while printing json data:|{data}|"
        JALogMsg( errorMsg, logFileName, True, True)
        
def JAProcessLibraryPanel( libraryPanelName, origUid, summaryLibraryPanelName, bucketNameSuffix ):
    """
    Get the details of library panel passed using origUid, update the it's values and
       create new summary library panel if one does not exist with new name
       update existing summary panel one exists already
    libaryPanel['uid'] = set this to None while creating new panel
    libraryPanel['model']['targets'] - replace bucket name for each target
    libraryPanel['model']['title'] - assign summaryLibraryPanelName
    libraryPanel['meta'] = set this to None

    Parms passed
        libraryPanelName - library panel that refers to the data in current or base bucket
        origUid - uid of library panel, used to GET panel information
        summaryLibraryPanelName - Name of the Library panel to be created or updated
        bucketNameSuffix - to be appended to form the bucket name reference in query

    Returns
        returnStatus - True or False
        newUid - Uid of new library panel created that refers to the bucket name passed
    """
    global debugLevel, JAConfigParams
    
    headers = {
        'Authorization': f"Bearer {JAConfigParams['GrafanaAPIKey']}",
        'Content-Type': 'application/json',
    }

    returnStatus = False

    if( debugLevel ) :
        errorMsg = f"DEBUG-1 JAProcessLibraryPanel() called with libraryPanelName:|{libraryPanelName}|, origUid:|{origUid}|, summaryLibraryPanelName:|{summaryLibraryPanelName}|, bucketNameSuffix:|{bucketNameSuffix}|"
        JALogMsg( errorMsg, logFileName, True, True)

    ### get the details of libraryPanel using passed origUid 
    try:
        response = requests.get(JAConfigParams['GrafanaAPIURL'] + 'library-elements/' + origUid, headers=headers, verify=False)
        response.raise_for_status()  # Raise an exception for 4xx/5xx errors
        result = response.json()
        libraryPanelDetails = result['result']

        if( debugLevel > 1 ) :
            errorMsg = f"DEBUG-2 JAProcessLibraryPanel() details of current libary panel:|{libraryPanelName}|\n{libraryPanelDetails}"
            JALogMsg( errorMsg, logFileName, True, True)

    except (requests.exceptions.RequestException, requests.exceptions.ConnectionError, requests.exceptions.HTTPError, requests.exceptions.MissingSchema) as err:
        errorMsg = f"ERROR JAProcessLibraryPanel() failed to extract the details of current library panel:|{libraryPanelName}|, error: |{err}|"
        JALogMsg( errorMsg, logFileName, True, True)
        JAStats['ERROR'] += 1
        return returnStatus, "ERROR"
    except Exception as err:
        errorMsg = f"ERROR JAProcessLibraryPanel() unknown exception while getting details of current library panel:|{libraryPanelName}|, err:|{err}|"
        JALogMsg( errorMsg, logFileName, True, True)
        JAStats['ERROR'] += 1
        return returnStatus, "ERROR"

    if( debugLevel > 3 ) :
        JAPrintJsonElements(libraryPanelDetails,0)

    ### change the bucket name to summary bucket name within this library panel's target queries
    try:
        if ('model' in libraryPanelDetails):
            tempModel = libraryPanelDetails['model']

            ### if datasource is loki, no change to the library panel reference for now
            ### in future, if summarized loki data can be done, then, enhance this section
            try:
                if ( 'datasource' in tempModel ) :
                    if ( 'type' in tempModel['datasource'] ) :
                        datasourceType = tempModel['datasource']['type']
                        if ( datasourceType == 'loki' ) :
                            if ( debugLevel ) :
                                errorMsg = f"DEBUG-1 JAProcessLibraryPanel() reference library panel:|{libraryPanelName}|, uid:|{origUid}|, uses datasourceType: 'loki', leaving the library panel reference as is"
                                JALogMsg( errorMsg, logFileName, True, True)
                            return True, origUid 
            except:
                if( debugLevel ):
                    errorMsg =  f"DEBUG-1 JAProcessLibraryPanel() no datasource or no datasource type in reference library panel:|{libraryPanelName}|, uid:|{origUid}|, libraryPanelDetails:|{libraryPanelDetails}|, skipping this."
                    JALogMsg( errorMsg, logFileName, True, True)

            try:
                if ('targets' in tempModel ):
                    returnStatus = JAProcessTargets("", libraryPanelName, tempModel['targets'], bucketNameSuffix )
                    if( returnStatus == False ) :
                        return returnStatus, "ERROR"
            except:
                if( debugLevel ):
                    errorMsg =  f"DEBUG-1 JAProcessLibraryPanel() no targets in reference library panel:|{libraryPanelName}|, uid:|{origUid}|, libraryPanelDetails:|{libraryPanelDetails}|, going to create/update summary library panel"
                    JALogMsg( errorMsg, logFileName, True, True)
    except:
        if( debugLevel ):
            errorMsg = f"DEBUG-1 JAProcessLibraryPanel() no model in reference library panel:|{libraryPanelName}|, uid:|{origUid}|, libraryPanelDetails:|{libraryPanelDetails}|"
            JALogMsg( errorMsg, logFileName, True, True)

    ### get the summary dashboard details if present
    newUid = origUid + f"-S-{bucketNameSuffix}"
    try:
        response = requests.get(JAConfigParams['GrafanaAPIURL'] + 'library-elements/' + newUid, headers=headers, verify=False)
        response.raise_for_status()  # Raise an exception for 4xx/5xx errors
        
        result = response.json()
        summaryLibraryPanelDetails = result['result']
        summaryLibraryPanelPresent = True

        if( debugLevel > 1 ) :
            errorMsg = f"DEBUG-2 JAProcessLibraryPanel() details of current summary libary panel:|{summaryLibraryPanelName}|\n{summaryLibraryPanelDetails}"
            JALogMsg( errorMsg, logFileName, True, True)

    except requests.exceptions.RequestException as err:
        summaryLibraryPanelPresent = False
        if( debugLevel ) :
            errorMsg = f"DEBUG-1 JAProcessLibraryPanel() failed to extract the details of summary library panel:|{summaryLibraryPanelName}|, error: |{err}|, proceeding to create the summary library panel"
            JALogMsg( errorMsg, logFileName, True, True)

    ### now if libaryPanelDetails and summaryPanelDetails differ, update summaryPanelDetails
    if( summaryLibraryPanelPresent == True ) :
        ### set the values to None for the fields where values are expected to be different
        tempId = summaryLibraryPanelDetails['id']
        tempMeta = copy.deepcopy(summaryLibraryPanelDetails['meta'])
        tempVersion = summaryLibraryPanelDetails['version']
        if ( 'model' in libraryPanelDetails ) :
            if ( 'libraryPanel' in libraryPanelDetails['model'] ) :
                if( 'meta' in libraryPanelDetails['model']['libraryPanel'] ) :
                    tempLibraryPanelMeta = copy.deepcopy(summaryLibraryPanelDetails['model']['libraryPanel']['meta'])
                    summaryLibraryPanelDetails['model']['libraryPanel']['meta'] = libraryPanelDetails['model']['libraryPanel']['meta'] = None
        summaryLibraryPanelDetails['id'] = libraryPanelDetails['id'] = None
        summaryLibraryPanelDetails['uid'] = libraryPanelDetails['uid'] = None
        summaryLibraryPanelDetails['name'] = libraryPanelDetails['name'] = None
        summaryLibraryPanelDetails['meta'] = libraryPanelDetails['meta'] = None
        summaryLibraryPanelDetails['version'] = libraryPanelDetails['version'] 

        if ( sorted(summaryLibraryPanelDetails.items()) != sorted(libraryPanelDetails.items()) ) :
            if( debugLevel > 1 ) :
                try:
                    import jsondiff 
                    libraryPanelDiff = jsondiff.diff(summaryLibraryPanelDetails, libraryPanelDetails)
                    errorMsg = f"DEBUG-2 JAProcessLibraryPanel() difference between library panel and summary library panel data: |{libraryPanelDiff}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                except:
                    errorMsg = f"DEBUG-2 JAProcessLibraryPanel() install jsondiff if you want to see the difference between library panel and summary library panels"
                    JALogMsg( errorMsg, logFileName, True, True)

            ### restore original values
            summaryLibraryPanelDetails['id'] = tempId
            summaryLibraryPanelDetails['uid'] = newUid
            summaryLibraryPanelDetails['name'] = summaryLibraryPanelName
            summaryLibraryPanelDetails['meta'] = copy.deepcopy( tempMeta )
            summaryLibraryPanelDetails['model']['libraryPanel']['meta'] = copy.deepcopy(tempLibraryPanelMeta)
            ### increment the version from previous version of summaryLibraryPanelDetails
            summaryLibraryPanelDetails['version'] = int(tempVersion) + 1

            if ( debugLevel ) :
                errorMsg = f"DEBUG-1 JAProcessLibraryPanel() updating summary library panel:|{summaryLibraryPanelName}|, uid:|{newUid}|, summaryLibraryPanelDetails:|{summaryLibraryPanelDetails}|"
                JALogMsg( errorMsg, logFileName, True, True)
            try:
                response = requests.patch(
                        JAConfigParams['GrafanaAPIURL'] + 'library-elements/' + newUid,
                        headers=headers,
                        data=json.dumps(summaryLibraryPanelDetails),
                        verify=False
                        )
                response.raise_for_status()  # Raise an exception for 4xx/5xx errors
                if( debugLevel ) :
                    errorMsg = f"DEBUG-1 JAProcessLibraryPanel() updated summary library panel:|{summaryLibraryPanelName}|, response:|{response}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                if ( response.status_code == 200 ):
                    returnStatus = True
                else:
                    errorMsg = f"ERROR JAProcessLibraryPanel() error updating the summary library panel:|{summaryLibraryPanelName}|, response.status_code:|{response.status_code}|, response:|{response}|"
                    JALogMsg( errorMsg, logFileName, True, True)

            except requests.exceptions.RequestException as err:
                errorMsg = f"ERROR JAProcessLibraryPanel() failed to update summary library panel:|{summaryLibraryPanelName}|, error: |{err}|"
                JALogMsg( errorMsg, logFileName, True, True)
        else:
            returnStatus = True
            if( debugLevel ) :
                errorMsg = f"DEBUG-1 JAProcessLibraryPanel() library panel:|{libraryPanelName}| and summary library panel:|{summaryLibraryPanelName}| are identical, no update needed to summary library panel"
                JALogMsg( errorMsg, logFileName, True, True)

    else:
        if( debugLevel > 3 ) :
            errorMsg = f"DEBUG-4 JAProcessLibraryPanel() details of reference libary panel:|{libraryPanelName}| used to create summary library panel:|{summaryLibraryPanelName}|"
            JALogMsg( errorMsg, logFileName, True, True)
            JAPrintJsonElements( libraryPanelDetails, 0)
        ### create summary library panel
        libraryPanelDetails['version'] = 0
        libraryPanelDetails['id'] = libraryPanelDetails['meta'] = None
        libraryPanelDetails['uid'] = newUid 
        libraryPanelDetails['name'] = summaryLibraryPanelName 
        if ( 'model' in libraryPanelDetails ) :
            if ( 'libraryPanel' in libraryPanelDetails['model'] ) :
                if( 'meta' in libraryPanelDetails['model']['libraryPanel'] ) :
                    libraryPanelDetails['model']['libraryPanel']['meta'] = None
        try:
            response = requests.post(
                    JAConfigParams['GrafanaAPIURL'] + 'library-elements',
                    headers=headers,
                    data=json.dumps(libraryPanelDetails),
                    verify=False
                    )
            response.raise_for_status()  # Raise an exception for 4xx/5xx errors
            if( debugLevel ) :
                errorMsg = f"DEBUG-1 JAProcessLibraryPanel() created the summary library panel:|{summaryLibraryPanelName}| with response.status_code:|{response.status_code}|"
                JALogMsg( errorMsg, logFileName, True, True)
            if ( response.status_code == 200 ):
                returnStatus = True
            else:
                errorMsg = f"ERROR JAProcessLibraryPanel() error creating the summary library panel:|{summaryLibraryPanelName}|, response.status_code:|{response.status_code}|, response:|{response}|"
                JALogMsg( errorMsg, logFileName, True, True)

        except requests.exceptions.RequestException as err:
            errorMsg = f"ERROR JAProcessLibraryPanel() failed to create summary library panel:|{summaryLibraryPanelName}|, error: |{err}|, response:|{response}|"
            JALogMsg( errorMsg, logFileName, True, True)

    if ( returnStatus == False ) :
        JAStats['ERROR'] += 1

    return returnStatus, newUid 

def JAUpdateDashboard( dashboardTitle, dashboardDetails ):
    """
    First check whether the dashboard with given title is already present. 
    If present, update that dashboard with new details passed.
    If not present, create new dashboard

    Parameters passed:
        dashboardTitle - this title is used to check for pre-existing dashboards with this title
        dashboardDetails - new details

    Returns:
        returnStatus - True or False
    """
    global debugLevel, summaryDashboardsHash, JAConfigParams
    
    headers = {
        'Authorization': f"Bearer {JAConfigParams['GrafanaAPIKey']}",
        'Content-Type': 'application/json',
    }

    returnStatus = False
    if ( debugLevel ) :
        errorMsg = f"DEBUG-1 JAUpdateDashboard() is called with dashboardTitle:|{dashboardTitle}|"
        JALogMsg( errorMsg, logFileName, True, True)

    summaryDashboardPresent = False
    try:
        if( dashboardTitle in summaryDashboardsHash ):
            summaryDashboardPresent = True
    except:
        if( debugLevel > 1 ):
            errorMsg = f"DEBUG-2 JAUpdateDashboard() summary dashboard not present with title:|{dashboardTitle}|, will create it new"
            JALogMsg( errorMsg, logFileName, True, True)
    if ( summaryDashboardPresent == True ):
        ### get the current summary dashboard details
        uid = summaryDashboardsHash[dashboardTitle] 
        response = requests.get(JAConfigParams['GrafanaAPIURL'] + 'dashboards/uid/' + uid, headers=headers, verify=False)
        response.raise_for_status()  # Raise an exception for 4xx/5xx errors
        
        summaryDashboardDetails = response.json()

        if( debugLevel > 1 ) :
            errorMSg = f"DEBUG-2 JAUpdateDashboard() details of current summary dashboard title:|{dashboardTitle}|\n{summaryDashboardDetails}"
            JALogMsg( errorMsg, logFileName, True, True)

        ### compare current summary dashboard to the dashboard data passed after normalizing
        ###   id, meta fields. If both are NOT identical, update the dashboard
        tempId = summaryDashboardDetails['dashboard']['id']
        tempMeta = copy.deepcopy(summaryDashboardDetails['meta'])
        tempVersion = summaryDashboardDetails['dashboard']['version']
        
        dashboardDetails['dashboard']['time'] = copy.deepcopy(summaryDashboardDetails['dashboard']['time'])
        summaryDashboardDetails['dashboard']['id'] = None
        summaryDashboardDetails['meta'] = None
        summaryDashboardDetails['dashboard']['overwrite'] = True

        dashboardDetails['dashboard']['id'] = None 
        dashboardDetails['meta'] = None
        ### set the id to the id of the summary dashboard that is already present
        dashboardDetails['dashboard']['uid'] = summaryDashboardsHash[dashboardTitle]
        ### set overwrite flag to True
        dashboardDetails['dashboard']['overwrite'] = True
        dashboardDetails['dashboard']['version'] = summaryDashboardDetails['dashboard']['version'] 

        tempGridPos = defaultdict(dict)
        tempFieldConfig = defaultdict(dict)

        for panel in summaryDashboardDetails['dashboard']['panels']:
            if ( 'gridPos' in panel ) :
                tempGridPos[ panel['title'] ] = copy.deepcopy(panel['gridPos'])
                panel['gridPos'] = None
            if ( 'fieldConfig' in panel ) :
                tempFieldConfig[ panel['title'] ] = copy.deepcopy(panel['fieldConfig'])
                panel['fieldConfig'] = None

        for panel in dashboardDetails['dashboard']['panels']:
            if ( 'gridPos' in panel ) :
                panel['gridPos'] = None
            if ( 'fieldConfig' in panel ) :
                panel['fieldConfig'] = None

        if ( sorted(summaryDashboardDetails.items()) != sorted(dashboardDetails.items()) ) :
            if( debugLevel > 1 ) :
                try:
                    import jsondiff 
                    ignoreFields = ['gridPos','fieldConfig','meta']
                    dashboardDiff = jsondiff.diff(summaryDashboardDetails, dashboardDetails,ignoreFields)
                    errorMsg = f"DEBUG-2 JAUpdateDashboard() difference between current dashboard and newly created dashboard data: |{dashboardDiff}|"
                    JALogMsg( errorMsg, logFileName, True, True)

                    JAPrintJsonElements(dashboardDiff,0)

                    errorMsg = f"DEBUG-2 JAUpdateDashboard() summaryDashboardDetails:"
                    JALogMsg( errorMsg, logFileName, True, True)
                    JAPrintJsonElements(summaryDashboardDetails,0)
                    errorMsg = f"DEBUG-2 JAUpdateDashboard() dashboardDetails:"
                    JALogMsg( errorMsg, logFileName, True, True)
                    JAPrintJsonElements(dashboardDetails,0)

                except:
                    errorMsg = f"DEBUG-2 JAUpdateDashboard() install jsondiff if you want to see the difference between current reference dashboard and previous summary dashboard"
                    JALogMsg( errorMsg, logFileName, True, True)

            dashboardDetails['dashboard']['version'] = int(tempVersion)+1 
            dashboardDetails['dashboard']['id'] = tempId
            # dashboardDetails['meta'] = copy.deepcopy(tempMeta)

            ### restore the gridPosition of summaryDashboardDetails saved before
            for panel in dashboardDetails['dashboard']['panels']:
                if ( 'gridPos' in panel ) :
                    panel['gridPos'] = copy.deepcopy(tempGridPos[ panel['title'] ])
                if ( 'fieldConfig' in panel ) :
                    panel['fieldConfig'] = copy.deepcopy(tempFieldConfig[ panel['title'] ])

            if ( debugLevel ) :
                errorMsg = f"DEBUG-1 JAUpdateDashboard() creating version:|{dashboardDetails['dashboard']['version']}| of summary dashboard with title:|{dashboardTitle}|, uid:|{summaryDashboardsHash[dashboardTitle]}|, dashboardDetails:|{dashboardDetails}|"
                JALogMsg( errorMsg, logFileName, True, True)
            try:
                response = requests.post(
                        JAConfigParams['GrafanaAPIURL'] + 'dashboards/db',
                        headers=headers,
                        data=json.dumps(dashboardDetails),
                        verify=False
                        )
                response.raise_for_status()  # Raise an exception for 4xx/5xx errors
                if ( response.status_code == 200 ):
                    JAStats['NewSummaryDashboards'] += 1
                    returnStatus = True
                    if( debugLevel ) :
                        errorMsg = f"DEBUG-1 JAUpdateDashboard() updated summary dashboard with title:|{dashboardTitle}|"
                        JALogMsg( errorMsg, logFileName, True, True)
                else:
                    errorMsg = f"ERROR JAUpdateDashboard() error updating the summary dashboard with title:|{dashboardTitle}|, response.status_code:|{response.status_code}|, response:|{response}|"
                    JALogMsg( errorMsg, logFileName, True, True)
            except requests.exceptions.RequestException as err:
                errorMsg = f"ERROR JAUpdateDashboard() failed to update summary dashboard with title:|{dashboardTitle}|, error: |{err}|, response:|{response}|\ndashboardDetails:"
                JALogMsg( errorMsg, logFileName, True, True)
                JAPrintJsonElements(dashboardDetails,0)
        else:
            if( debugLevel ) :
                errorMsg = f"DEBUG-1 JAUpdateDashboard() new summary dashboard title:|{dashboardTitle}|, data is identical to current summary dashboard, dashboard NOT updated"
                JALogMsg( errorMsg, logFileName, True, True)

            JAStats['NoChangeToSummaryDashboard'] += 1
            returnStatus = True
    else:
        try:
            ### set the id and uid to None for new dashboard to be created
            dashboardDetails['dashboard']['id'] = None
            dashboardDetails['dashboard']['uid'] = None
            dashboardDetails['dashboard']['version'] = 0 
            dashboardDetails['dashboard']['overwrite'] = False
            dashboardDetails['meta'] = None

            ### set default ['time']['from'] depending on summary dashboard type
            if( 'SummaryDashboardTimeFrom' in JAConfigParams ):
                if( debugLevel > 2 ) :
                    errorMsg = f"DEBUG-3 JAUpdateDashboard() JAConfigParams['SummaryDashboardTimeFrom']:|{JAConfigParams['SummaryDashboardTimeFrom']}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                tempTimeFromSpec = JAConfigParams['SummaryDashboardTimeFrom']
                for key, value in tempTimeFromSpec.items(): 
                    if( debugLevel > 2 ) :
                        errorMsg = f"DEBUG-3 JAUpdateDashboard() JAConfigParams['SummaryDashboardTimeFrom'] key:|{key}|, value:|{value}|"
                        JALogMsg( errorMsg, logFileName, True, True)
                    if( re.search(key, dashboardDetails['dashboard']['title']) != None ):
                        ### use the value as default time from for the dashboard
                        dashboardDetails['dashboard']['time']['from'] = value
                        if( debugLevel > 1 ) :
                            errorMsg = f"DEBUG-2 JAUpdateDashboard() JAConfigParams['SummaryDashboardTimeFrom'] using the key:|{key}|, value:|{value}| as default ['time']['from'] for the dashboard"
                            JALogMsg( errorMsg, logFileName, True, True)

                        break

            if ( debugLevel ) :
                errorMsg = f"DEBUG-1 JAUpdateDashboard() creating summary dashboard with title:|{dashboardTitle}|, dashboardDetails:|{dashboardDetails}|"
                JALogMsg( errorMsg, logFileName, True, True)
            try:
                # Create the dashboard
                response = requests.post(
                            JAConfigParams['GrafanaAPIURL'] + 'dashboards/db',
                            headers=headers,
                            data=json.dumps(dashboardDetails),
                            verify=False
                            )
                response.raise_for_status()  # Raise an exception for 4xx/5xx errors
                if ( response.status_code == 200 ):
                    returnStatus = True
                    if( debugLevel ) :
                        errorMsg = f"DEBUG-1 JAUpdateDashboard() created summary dashboard with title:|{dashboardTitle}|"
                        JALogMsg( errorMsg, logFileName, True, True)
                    JAStats['NewSummaryDashboards'] += 1
                else:
                    errorMsg = f"ERROR JAUpdateDashboard() error creating summary dashboard with title:|{dashboardTitle}|, response.status_code:|{response.status_code}|, response:|{response}|\n"
                    JALogMsg( errorMsg, logFileName, True, True)

            except requests.exceptions.RequestException as err:
                errorMsg = f"ERROR JAUpdateDashboard() failed to create the summary dashboard with title:|{dashboardTitle}|, error: |{err}|"
                JALogMsg( errorMsg, logFileName, True, True)
        except:
            errorMsg = f"ERROR JAUpdateDashboard() failed to create the summary dashboard with title:|{dashboardTitle}|, could not set time from, id, uid and version properties, dashboardDetails: |{dashboardDetails}|"
            JALogMsg( errorMsg, logFileName, True, True)

    return returnStatus

def JAProcessTargets( dashboardTitle, panelTitle, targets, bucketNameSuffix ) :
    """
    This function replaces the bucket name with bucketNameSuffix passed within query param of target 
    """
    global debugLevel, JAStats

    returnStatus = True
    try:
        for target in targets:
            try:
                targetDatasource = target.get('datasource', '')
                targetDatasourceType = targetDatasource['type'] 
                if( debugLevel > 1 ) :
                    errorMsg = f"DEBUG-2 JAProcessTargets() Datasource: |{targetDatasource}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                    errorMsg = f"DEBUG-2 JAProcessTargets() DatasourceType: |{targetDatasourceType}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                if( targetDatasourceType == 'influxdb' ) :
                    ### change the bucket name used in the query
                    targetQuery = target.get('query', '')
                    targetRefId = target.get('refId', '')
                    if( debugLevel > 1 ) :
                        errorMsg = f"DEBUG-2 JAProcessTargets() Query: |{targetQuery}|"
                        JALogMsg( errorMsg, logFileName, True, True)
                    try:
                        queryParts = re.split( r"from(\s*)\(bucket: \"(\w+)\"\)", targetQuery)
                            
                        if ( len(queryParts) < 3 ):
                            if ( len(targetQuery) > 0 ): 
                                errorMsg = f"ERROR JAProcessTargets() dashboard: |{dashboardTitle}|, panel:|{panelTitle}|, refId:|{targetRefId}|, influxdb query:|{targetQuery}|, is not in expected format, target: |{target}|"
                                JALogMsg( errorMsg, logFileName, True, True)
                                JAStats['ERROR'] += 1
                            else:
                                errorMsg = f"INFO JAProcessTargets() dashboard: |{dashboardTitle}|, panel:|{panelTitle}|, refId:|{targetRefId}|, influxdb query:|{targetQuery}|, ingnoring empty query"
                                JALogMsg( errorMsg, logFileName, True, True)
                        else:
                            if( len(queryParts) == 3 ) :
                                ### query in the form: from(bucket...  (no space between from and (bucket
                                if( debugLevel > 2 ) :
                                    errorMsg = f"DEBUG-3 JAProcessTargets() queryParts [0]: |{queryParts[0]}| from (bucket:, [1]:\"|{queryParts[1]}|\", [2]: |{queryParts[2]}|"
                                    JALogMsg( errorMsg, logFileName, True, True)
                                ### replace current bucket name with summary bucket name
                                queryParts[1] = f"{queryParts[1]}{bucketNameSuffix}"
                                ### Now prepare the updated targetQuery string
                                targetQuery = f"{queryParts[0]}from (bucket: \"{queryParts[1]}\"){queryParts[2]}"
                            elif ( len(queryParts) == 4 ) :
                                ### query in the form: from (bucket...
                                if( debugLevel > 2 ) :
                                    errorMsg = f"DEBUG-3 JAProcessTargets() queryParts [0]: |{queryParts[0]}| from (bucket:, [2]:\"|{queryParts[2]}|\", [3]: |{queryParts[3]}|"
                                    JALogMsg( errorMsg, logFileName, True, True)
                                ### replace current bucket name with summary bucket name
                                queryParts[1] = f"{queryParts[2]}{bucketNameSuffix}"
                                ### Now prepare the updated targetQuery string
                                targetQuery = f"{queryParts[0]}from (bucket: \"{queryParts[1]}\"){queryParts[3]}"

                            try:
                                target['query'] = targetQuery
                                if( debugLevel > 2 ) :
                                    errorMsg = f"DEBUG-3 JAProcessTargets() updated query with summary bucket name: |{target['query']}|"
                                    JALogMsg( errorMsg, logFileName, True, True)
                                JAStats['SummaryTargets'] += 1
                            except:
                                errorMsg = f"ERROR JAProcessTargets() name: |{dashboardTitle}|, panel:|{panelTitle}|, refId:|{targetRefId}|, error setting the query string of target: |{target}|"
                                JALogMsg( errorMsg, logFileName, True, True)
                                JAStats['ERROR'] += 1
                    except:
                        errorMsg = f"ERROR JAProcessTargets() name: |{dashboardTitle}|, , panel:|{panelTitle}|, error parsing the query:|{queryParts}| of target:|{target}|"
                        JALogMsg( errorMsg, logFileName, True, True)

            except:
                errorMsg = f"ERROR JAProcessTargets() name: |{dashboardTitle}|, , panel:|{panelTitle}|, error getting the details of target: |{target}|\n"
                JALogMsg( errorMsg, logFileName, True, True)
                JAStats['ERROR'] += 1
    except:
        errorMsg = f"ERROR JAProcessTargets() dashboard: |{dashboardTitle}|, , panel:|{panelTitle}|, error processing targets:{targets}\n"
        JALogMsg( errorMsg, logFileName, True, True)
        JAStats['ERROR'] += 1


def JAProcessTemplates(dashboardTitle, templates, bucketNameSuffix ) :
    """
    This function replaces the bucket name with bucketNameSuffix passed within definition and query parameters
    """
    global debugLevel, JAStats

    returnStatus = True
    try:
        for template in templates:
            try:
                templateDatasource = template.get('datasource', '')
                templateDatasourceType = templateDatasource['type'] 
                if( debugLevel > 1 ) :
                    errorMsg = f"DEBUG-2 JAProcessTemplates() Datasource: |{templateDatasource}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                    errorMsg = f"DEBUG-2 JAProcessTemplates() DatasourceType: |{templateDatasourceType}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                if( templateDatasourceType == 'influxdb' ) :
                    for paramType in [ 'query', 'definition' ]:
                        if paramType in template:
                            ### change the bucket name used in the query
                            templateQuery = template.get(paramType, '')
                            if( debugLevel > 1 ) :
                                errorMsg = f"DEBUG-2 JAProcessTemplates() {paramType}: |{templateQuery}|"
                                JALogMsg( errorMsg, logFileName, True, True)
                            try:
                                queryParts = re.split( r"from(\s*)\(bucket: \"(\w+)\"\)", templateQuery)

                                if ( len(queryParts) < 3 ):
                                    errorMsg = f"ERROR JAProcessTemplates() influxdb query is not in expected format, not able to create summary template for the template: |{template}|"
                                    JALogMsg( errorMsg, logFileName, True, True)
                                    JAStats['ERROR'] += 1
                                else:
                                    if ( len(queryParts) == 3 ) :
                                        if( debugLevel > 2 ) :
                                            errorMsg = f"DEBUG-3 JAProcessTemplates() queryParts [0]: |{queryParts[0]}| from (bucket:, [1]:\"|{queryParts[1]}|\", [2]: |{queryParts[2]}|"
                                            JALogMsg( errorMsg, logFileName, True, True)
                                        ## query format: from(bucket... <-- NO space between from and (bucket
                                        ### replace current bucket name with summary bucket name
                                        queryParts[1] = f"{queryParts[1]}{bucketNameSuffix}"
                                        ### Now prepare the updated targetQuery string
                                        templateQuery = f"{queryParts[0]}from (bucket: \"{queryParts[1]}\"){queryParts[2]}"
                                    elif ( len(queryParts) ==  4 ) :
                                        if( debugLevel > 2 ) :
                                            errorMsg = f"DEBUG-3 JAProcessTemplates() queryParts [0]: |{queryParts[0]}| from (bucket:, [2]:\"|{queryParts[2]}|\", [3]: |{queryParts[3]}|"
                                            JALogMsg( errorMsg, logFileName, True, True)
                                        ## query format: from (bucket... <-- space between from and (bucket
                                        ### replace current bucket name with summary bucket name
                                        queryParts[2] = f"{queryParts[2]}{bucketNameSuffix}"
                                        ### Now prepare the updated targetQuery string
                                        templateQuery = f"{queryParts[0]}from (bucket: \"{queryParts[2]}\"){queryParts[3]}"
                                    try:
                                        template[paramType] = templateQuery
                                        if( debugLevel > 2 ) :
                                            errorMsg = f"DEBUG-3 JAProcessTemplates() updated {paramType} with summary bucket name: |{template[paramType]}|"
                                            JALogMsg( errorMsg, logFileName, True, True)
                                        JAStats['SummaryTemplates'] += 1
                                    except:
                                        errorMsg = f"ERROR JAProcessTemplates() error setting the {paramType} string of template: |{template}|, templateQuery:|{templateQuery}|"
                                        JALogMsg( errorMsg, logFileName, True, True)
                                        JAStats['ERROR'] += 1
                            except:
                                errorMsg = f"ERROR JAProcessTemplates() error parsing the {paramType}:|{queryParts}| of template:|{template}|"
                                JALogMsg( errorMsg, logFileName, True, True)
            except:
                errorMsg = f"ERROR JAProcessTemplates() dashboard: |{dashboardTitle}| error getting the details of template: |{template}|\n"
                JALogMsg( errorMsg, logFileName, True, True)
                JAStats['ERROR'] += 1
    except:
        errorMsg = f"ERROR JAProcessTemplates() dashboard: |{dashboardTitle}|, error processing the templates:|{templates}|"
        JALogMsg( errorMsg, logFileName, True, True)

    return returnStatus

def JAManageDashboards(makeSummaryDashboardsHash):
    """
    When called with makeSummaryDashboardsHash of True,
        it gets the list of all dashboards
        if the dashboard title contains the signature of summary dashboard,
          dashboard title and UID are stored in summaryDashboardsHash

    When called with makeSummaryDashboardsHash of False,
        it gets the list of all dashboards
        if the dashboard title contains the signature of summary dashboard,
            skips the processing
        else
            for each panel of the dashboard
                if panel refers to the library panel
                    if the summary library panel does not exist,
                        create it
                    populate panel with summary panel info
                else
                    for all targets of the panel
                        if the datasource type is 'influxdb'
                            change the bucket name to the name with summary bucket name
            calls JAUpdateDashboard() to apply the changes to the dashboard.
    """
    global JAConfigParams, JAStats, debugLevel, summaryDashboardsHash, headers
    global regexSearchForDashboardTitle

    headers = {
        'Authorization': f"Bearer {JAConfigParams['GrafanaAPIKey']}",
        'Content-Type': 'application/json',
    }

    try:
        response = requests.get(
                JAConfigParams['GrafanaAPIURL'] + 'search', headers=headers, verify=False)
        response.raise_for_status()  # Raise an exception for 4xx/5xx errors

        dashboards = response.json()

        referenceLibraryPanelsHash = defaultdict(dict) 

        ### this hash contains the name and uid of existing library panels
        ## this is used to update existing library panels with latest details
        ## ['uid'] - uid of summary library panel
        ## ['connections'] = [] - list of dashboard panel ids where this library panel is referred
        ##   while creating library summary panels, id of linked panels are stored here
        ##   at the end, after processing all dashboards, connections property of library panel will be updated
        libraryPanelsHash = defaultdict(dict)

        # Print information about each dashboard
        for dashboard in dashboards:
            processDashboard = True
            ### apply include and exclude dashboard specs
            if ( 'IncludeDashboards' in JAConfigParams):
                if( JAConfigParams['IncludeDashboards'] != 'all' and JAConfigParams['IncludeDashboards'] != 'All' and JAConfigParams['IncludeDashboards'] != 'ALL' ):
                    if( re.search(JAConfigParams['IncludeDashboards'], dashboard['title']) == None ): 
                        processDashboard = False
                        if( debugLevel ):
                            errorMsg = f"DEBUG-1 JAManageDashboards() dashboard title:|{dashboard['title']}| does not match to 'IncludeDashboards' spec:|{JAConfigParams['IncludeDashboards']}|, skipping this dashboard"
                            JALogMsg( errorMsg, logFileName, True, True)

            if ( 'ExcludeDashboards' in JAConfigParams):
                if( JAConfigParams['ExcludeDashboards'] != 'none' and JAConfigParams['ExcludeDashboards'] != 'None' and JAConfigParams['ExcludeDashboards'] != 'NONE' ):
                    if( re.search(JAConfigParams['ExcludeDashboards'], dashboard['title']) != None ): 
                        processDashboard = False 
                        if( debugLevel ):
                            errorMsg = f"DEBUG-1 JAManageDashboards() dashboard title:|{dashboard['title']}| matches to the 'ExcludeDashboards' spec:|{JAConfigParams['ExcludeDashboards']}|, skipping this dashboard"
                            JALogMsg( errorMsg, logFileName, True, True)

            if( processDashboard == False ) :
                continue

            if ( makeSummaryDashboardsHash == True ) :
                returnStatus = regexSearchForDashboardTitle.search(dashboard['title'])
                if( returnStatus != None and returnStatus != 0 ) :
                    ### store uid of the summary dashboard
                    summaryDashboardsHash[dashboard['title']] = dashboard['uid']
                    JAStats['OldSummaryDashboards'] += 1
                continue 

            errorMsg = f"INFO JAManageDashboards() Dashboard id: |{dashboard['id']}|, Title: |{dashboard['title']}|, uid: |{dashboard['uid']}|"
            JALogMsg( errorMsg, logFileName, True, True)
            JAStats['TotalDashboards'] += 1

            returnStatus = regexSearchForDashboardTitle.search(dashboard['title'])
            if( returnStatus != None and returnStatus != 0 ) :
                ### skip the summary dashboard, this will be recreated later
                if ( debugLevel ) :
                    errorMsg = f"DEBUG-1 JAManageDashboards() existing summary dashboard: |{dashboard['title']}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                JAStats['SkippedSummaryDashboards'] += 1
            else:
                JAStats['NonSummaryDashboards'] += 1
                if ( debugLevel ) :
                    errorMsg = f"DEBUG-1 JAManageDashboards() non-summary dashboard: |{dashboard['title']}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                try:
                    uid = dashboard['uid']
                    response = requests.get(JAConfigParams['GrafanaAPIURL'] + 'dashboards/uid/' + uid, headers=headers, verify=False)
                    response.raise_for_status()  # Raise an exception for 4xx/5xx errors

                    if( debugLevel ) :
                        errorMsg = f"DEBUG-1 JAManageDashboards() details of non-summary dashboard title: |{dashboard['title']}|, uid:|{uid}|"
                        JALogMsg( errorMsg, logFileName, True, True)


                    ### Now repleace influxdb bucket name with summary bucket name
                    ###  for each group & each value type, and save the dashboard details
                    if 'title' in dashboard:
                        referenceDashboardTitle = dashboard['title']
                    else:
                        referenceDashboardTitle = None

                    for group in JAConfigParams['SummaryGroups']:
                        for valueType in JAConfigParams['SummaryValueTypes']:
                            dashboardTitle = f"{referenceDashboardTitle} -S-{group}{valueType}"
                            bucketNameSuffix = f"{group}{valueType}"
                            dashboardDetails = response.json()
                            dashboardDetails['dashboard']['title'] = dashboardTitle
                            if( debugLevel > 3 ) :
                                try:
                                    JAPrintJsonElements(dashboardDetails,0)
                                except:
                                    errorMsg = "ERROR printing elements of json data"
                                    JALogMsg( errorMsg, logFileName, True, True)
                            if( debugLevel > 1 ) :
                                errorMsg = f"DEBUG-2 JAManageDashboards() dashboard title:|{dashboardTitle}|, details:|{dashboardDetails}|"
                                JALogMsg( errorMsg, logFileName, True, True)
                            try:
                                if ( 'panels' in dashboardDetails['dashboard'] ) :
                                    panels = dashboardDetails['dashboard']['panels']
                                else:
                                    panels = []
                                    errorMsg = f"INFO JAManageDashboards() no panels in dashboard with title: |{dashboardTitle}|\n\n"
                                    JALogMsg( errorMsg, logFileName, True, True)
                                    JAStats['INFO'] += 1
                                    continue

                            except:
                                panels = []
                                errorMsg = f"INFO JAManageDashboards() dashboard: |{dashboardTitle}| no panels in dashboard with title: |{dashboardTitle}|\n\n"
                                JALogMsg( errorMsg, logFileName, True, True)
                                JAStats['INFO'] += 1
                                continue
                            try:
                                # Iterate through panels to extract target details
                                for panel in panels:
                                    try:
                                        if 'title' in panel:
                                            panelTitle = panel['title']
                                        else:
                                            panelTitle = None

                                        if( debugLevel ) :
                                            errorMsg = f"DEBUG-1 JAManageDashboards() Panel['title']: |{panelTitle}|"
                                            JALogMsg( errorMsg, logFileName, True, True)
                                    except:
                                        panelTitle = None
                                        errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error getting panel title for panel:{panel}\n"
                                        JALogMsg( errorMsg, logFileName, True, True)
                                        JAStats['ERROR'] += 1
                                        continue

                                    try:
                                        if ( 'libraryPanel' in panel):
                                            try:
                                                ### current panel is library panel, 
                                                ### store the library panel info if not known already
                                                if 'name' in panel['libraryPanel']:
                                                    libraryPanelName = panel['libraryPanel']['name']
                                                else:
                                                    libraryPanelName = None

                                                if 'uid' in panel['libraryPanel']:
                                                    libraryPanelUid = panel['libraryPanel']['uid']
                                                else:
                                                    libraryPanelUid = None
                                            except :
                                                errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error getting libraryPanel name & uid for panel: |{panel}|\n\n"
                                                JALogMsg( errorMsg, logFileName, True, True)
                                                libraryPanelName = None
                                                JAStats['ERROR'] += 1
                                                continue
                                            

                                            if libraryPanelName is not None:
                                                if libraryPanelName not in referenceLibraryPanelsHash:
                                                    ### this section is used to count the library panel references
                                                    referenceLibraryPanelsHash[libraryPanelName] = libraryPanelName
                                                    JAStats['LibraryPanels'] += 1
                                                try:
                                                    summaryLibraryPanelName = f"{libraryPanelName} -S-{bucketNameSuffix}" 
                                                    if summaryLibraryPanelName not in libraryPanelsHash:
                                                        if( debugLevel) :
                                                            errorMsg = f"DEBUG-1 JAManageDashboards() creating/updating the summaryLibraryPanel: |{summaryLibraryPanelName}|, from reference libraryPanel: |{libraryPanelName}|, with uid: |{libraryPanelUid}|"
                                                            JALogMsg( errorMsg, logFileName, True, True)
                                                        returnStatus = False
                                                        try:
                                                            ### create or update summary library panel with reference to summary bucket name
                                                            returnStatus, newUid = JAProcessLibraryPanel( libraryPanelName, libraryPanelUid, summaryLibraryPanelName, bucketNameSuffix )
                                                            if returnStatus == True:
                                                                if( newUid == libraryPanelUid ) :
                                                                    ### continue to use current library panel as is in summary dashboard also
                                                                    continue

                                                                else:
                                                                    libraryPanelsHash[summaryLibraryPanelName]['uid'] = newUid 
                                                            else:
                                                                errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| not able to create/update summary libraryPanel: |{summaryLibraryPanelName}|, for reference library panel: |{libraryPanelName}|\n"
                                                                JALogMsg( errorMsg, logFileName, True, True)
                                                                JAStats['ERROR'] += 1
                                                                continue
                                                        except:
                                                            returnStatus = False
                                                            errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error creating summary libraryPanel:|{summaryLibraryPanelName}|, for reference library panel: |{libraryPanelName}|\n"
                                                            JALogMsg( errorMsg, logFileName, True, True)
                                                            JAStats['ERROR'] += 1
                                                            continue
                                                    else:
                                                        returnStatus = True
                                                        if( debugLevel) :
                                                            errorMsg = f"DEBUG-1 JAManageDashboards() summary library panel: |{summaryLibraryPanelName}| for library panel:|{libraryPanelName}| has been created/updated already, going to use it in the dashboard"
                                                            JALogMsg( errorMsg, logFileName, True, True)
                                                        
                                                    if returnStatus == True:
                                                        try:
                                                            ### replace the library panel name and UID references
                                                            ###  to corresponding summary library panel
                                                            # panel['title'] = panelTitle + ' -S-' + bucketNameSuffix
                                                            panel['libraryPanel']['name'] = summaryLibraryPanelName
                                                            panel['libraryPanel']['uid'] = libraryPanelsHash[summaryLibraryPanelName]['uid']
                                                            JAStats['SummaryLibraryPanels'] += 1
                                                        except:
                                                            errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error updating library panel:|{libraryPanelName}| for the panel: |{panel}|, with summary library panel:|{summaryLibraryPanelName}|\n"
                                                            JALogMsg( errorMsg, logFileName, True, True)
                                                            JAStats['ERROR'] += 1
                                                            continue

                                                except :
                                                    errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error processing libraryPanel: |{libraryPanelName}|\n"
                                                    JALogMsg( errorMsg, logFileName, True, True)
                                                    JAStats['ERROR'] += 1
                                                    continue

                                                if( debugLevel ) :
                                                    errorMsg = f"DEBUG-1 JAManageDashboards() updated panel with new library panel info:|{panel}|"
                                                    JALogMsg( errorMsg, logFileName, True, True)

                                            ### done processing current panel
                                            ###  no targets to process for library panel
                                            continue

                                    except:
                                        if ( debugLevel ) :
                                            errorMsg = f"DEBUG-2 JAManageDashboards() no libraryPanel in panel: |{panelTitle}|"
                                            JALogMsg( errorMsg, logFileName, True, True)
                                        continue
                                    
                                    ### process all targets within the current non-library panel
                                    ###   library panel referred in dashboard will not have targets property
                                    ###   as part of the dashboard where library panels are used. Those target
                                    ###   details will be with library panel definition JSON itself.
                                    if 'targets' in panel:
                                        JAProcessTargets(dashboardTitle, panelTitle, panel['targets'], bucketNameSuffix )
                                    JAStats['SummaryPanels'] += 1

                            except:
                                errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error getting the details of panels: |{panels}|\n\n"
                                JALogMsg( errorMsg, logFileName, True, True)
                                JAStats['ERROR'] += 1

                            ### now process templating if any
                            try:
                                if ( 'templating' in dashboardDetails['dashboard'] ) :
                                    templating = dashboardDetails['dashboard']['templating']
                                    if ( 'list' in templating ) :
                                        try:
                                            tempList = templating['list']
                                            JAProcessTemplates(dashboardTitle, tempList, bucketNameSuffix )
                                        except:
                                            errorMsg = f"ERROR JAManageDashboards() dashboard: |{dashboardTitle}| error extracting 'list' from templating:|{templating}|"
                                            JALogMsg( errorMsg, logFileName, True, True)
                            except:
                                if ( debugLevel > 1 ):
                                    errorMsg = f"DEBUG-2 JAManageDashboards() no templates in current dashboard:|{dashboardTitle}|"
                                    JALogMsg( errorMsg, logFileName, True, True)
                            
                            ### now, create or update the summary dashboards
                            if( debugLevel > 1 ):
                                errorMsg = f"DEBUG-2 JAManageDashboards() dashboard: |{dashboardTitle}|, panel['title']: |{panel['title']}|, targets: |{panel['targets']}|"
                                JALogMsg( errorMsg, logFileName, True, True)

                            returnStatus = JAUpdateDashboard( dashboardTitle, dashboardDetails )
                            if ( returnStatus  == False ) :
                                JAStats['ERROR'] += 1

                            ### if MaxSummaryDashboards property is defined in config file and
                            ###    number of summary dashboards processed match the max limit, get out.
                            if ( 'MaxSummaryDashboards' in JAConfigParams):
                                if( JAConfigParams['MaxSummaryDashboards'] > 0 ) :
                                    if ( JAStats['NewSummaryDashboards'] >= JAConfigParams['MaxSummaryDashboards'] ) :
                                        errorMsg = f"INFO Reached max limit of new summary dashboard create/update:|{JAConfigParams['MaxSummaryDashboards']}|"
                                        JALogMsg( errorMsg, logFileName, True, True)
                                        return

                except requests.exceptions.RequestException as err:
                    errorMsg = f"ERROR JAManageDashboards() failed to fetch details of dashboard: |{dashboard['title']}|, uid: |{dashboard['uid']}|, error: |{err}|"
                    JALogMsg( errorMsg, logFileName, True, True)
                    JAStats['ERROR'] += 1


    except requests.exceptions.RequestException as e:
        errorMsg = f"ERROR JAManageDashboards() error fetching dashboards: |{e}|"
        JALogMsg( errorMsg, logFileName, True, True)
        JAStats['ERROR'] += 1

    if ( makeSummaryDashboardsHash == True ) :
        if ( debugLevel ):
            errorMsg = f"DEBUG-1 JAManageDashboards() summary dashboards title,            uid:"
            JALogMsg( errorMsg, logFileName, True, True)
            for key in summaryDashboardsHash:
                errorMsg = "DEBUG-1 JAManageDashboards() %32s, %s" % ( key, summaryDashboardsHash[key])
                JALogMsg( errorMsg, logFileName, True, True)

def JASignalHandler(sig, frame):
    JAExit("Control-C pressed")

def JAExit(reason):
    print(reason)
    global startTimeInSec 
    endTimeInSec = time.time()
    durationInSec = endTimeInSec - startTimeInSec
    if( reason != '' ):
        reason = reason + ", "
    JALogMsg('{0} processing duration: {1} sec\n'.format(
        reason, durationInSec), JAConfigParams['LogFileName'], True)
    sys.exit()

def JAGatherEnvironmentSpecs(key, values, debugLevel):
    """
    parse the key / value pairs and assign to JAConfigParams dictionary
    """
    global JAConfigParams 

    for myKey, myValue in values.items():
        if debugLevel > 1:
            errorMsg = 'DEBUG-2 JAGatherEnvironmentSpecs() key: |{0}|, value: |{1}|'.format(myKey, myValue)
            JALogMsg( errorMsg, logFileName, True, True)

        ### non-number type of param definition
        if myKey in ['GrafanaAPIURL', 'GrafanaAPIKey', 'SummaryGroups', 'SummaryValueTypes', 'SummaryDashboardTimeFrom', 'MaxSummaryDashboards', 
                     'IncludeDashboards','ExcludeDashboards', 
                     'InfluxdbURL', 'InfluxdbToken', 'InfluxdbOrg', 'InfluxdbManageSummaryBuckets', 'InfluxdbBucketRetentionRules',
                     'IncludeBuckets', 'ExcludeBuckets']:
            if myValue != None:
                JAConfigParams[myKey] = myValue 

        ### integer type parameter
        if myKey == 'DebugLevel':
            if myValue != None:
                JAConfigParams[myKey] = int(myValue) 


def JAYamlLoad(fileName:str ):
    """
    Basic function to read config file in yaml format
    Use this on host without python 3 or where yaml is not available

    Upon successful read, returns the yaml data in dictionary form
    """

    from collections import defaultdict
    import re
    yamlData = defaultdict(dict)
    paramNameAtDepth = {0: '', 1: '', 2: '', 3:'', 4: ''}
    leadingSpacesAtDepth = {0: 0, 1: None, 2: None, 3: None, 4: None}
    prevLeadingSpaces = 0
    currentDepth = 0
    currentDepthKeyValuePairs = defaultdict(dict)

    try:
        with open(fileName, "r") as file:
            depth = 1

            while True:
                tempLine =  file.readline()
                if not tempLine:
                    break
                # SKIP comment line
                if re.match(r'\s*#', tempLine):
                    continue

                tempLine = tempLine.rstrip("\n")
                if re.match("---", tempLine) :
                    continue
                if len(tempLine) == 0:
                    continue
                # remove leading and trailing spaces, newline
                lstripLine = tempLine.lstrip()
                if len(lstripLine) == 0:
                    continue

                ## separate param name and value, split to two parts, the value itself may have ':'
                params = lstripLine.split(':', 1)
                ## remove leading space from value field
                params[1] = params[1].lstrip()

                # based on leading spaces, determine depth
                leadingSpaces = len(tempLine)-len(lstripLine)
                if leadingSpaces == prevLeadingSpaces:

                    if leadingSpaces == 0:
                        if params[1] == None or len(params[1]) == 0 :
                            # if value does not exist, this is the start of parent/child definition
                            paramNameAtDepth[currentDepth+1] = params[0]

                        else:
                            # top layer, assign the key, value pair as is to yamlData
                            yamlData[params[0]] = params[1]
                    else:
                        # store key, value pair with current depth dictionary
                        currentDepthKeyValuePairs[params[0]] = params[1]

                    leadingSpacesAtDepth[currentDepth+1] = leadingSpaces

                elif leadingSpaces < prevLeadingSpaces:
                    # store key, value pair of prev depth 
                    for key, values in currentDepthKeyValuePairs.items():
                        if currentDepth == 1:
                            if paramNameAtDepth[1] not in yamlData.keys() :
                                yamlData[ paramNameAtDepth[1]] = {}
                            
                            yamlData[ paramNameAtDepth[1] ][key] = values

                        elif currentDepth == 2:
                            if paramNameAtDepth[1] not in yamlData.keys() :
                                yamlData[ paramNameAtDepth[1]] = {}
                            if paramNameAtDepth[2] not in yamlData[paramNameAtDepth[1]].keys() :
                                yamlData[ paramNameAtDepth[1]][paramNameAtDepth[2]] = {}

                            yamlData[paramNameAtDepth[1]][paramNameAtDepth[2]][key] = values
                        elif currentDepth == 3:
                            if paramNameAtDepth[1] not in yamlData.keys() :
                                yamlData[ paramNameAtDepth[1]] = {}
                            if paramNameAtDepth[2] not in yamlData[paramNameAtDepth[1]].keys() :
                                yamlData[ paramNameAtDepth[1]][paramNameAtDepth[2]] = {}
                            if paramNameAtDepth[3] not in yamlData[paramNameAtDepth[1]][paramNameAtDepth[2]].keys() :
                                yamlData[ paramNameAtDepth[1]][paramNameAtDepth[2]][paramNameAtDepth[3]] = {}
                            yamlData[paramNameAtDepth[1]][paramNameAtDepth[2]][paramNameAtDepth[3]][key] = values

                    currentDepthKeyValuePairs = defaultdict(dict)
                    
                    if leadingSpacesAtDepth[currentDepth-1] == leadingSpaces:
                        currentDepth -= 1
                    elif leadingSpacesAtDepth[currentDepth-2] == leadingSpaces:
                        currentDepth -= 2
                    elif leadingSpacesAtDepth[currentDepth-3] == leadingSpaces:
                        currentDepth -= 3
                    prevLeadingSpaces = leadingSpaces

                    if params[1] == None or len(params[1]) == 0 :
                        # if value does not exist, this is the start of parent/child definition
                        paramNameAtDepth[currentDepth+1] = params[0]
                elif leadingSpaces > prevLeadingSpaces:
                    leadingSpacesAtDepth[currentDepth+1] = leadingSpaces
                    currentDepth += 1
                    prevLeadingSpaces = leadingSpaces
                    if params[1] == None or len(params[1]) == 0 :
                        # if value does not exist, this is the start of parent/child definition
                        paramNameAtDepth[currentDepth+1] = params[0]
                    else:
                        # save current key, value 
                        currentDepthKeyValuePairs[params[0]] = params[1]

            for key, values in currentDepthKeyValuePairs.items():
                if currentDepth == 1:
                    if paramNameAtDepth[1] not in yamlData.keys() :
                        yamlData[ paramNameAtDepth[1]] = {}
                            
                    yamlData[ paramNameAtDepth[1] ][key] = values

                elif currentDepth == 2:
                    if paramNameAtDepth[1] not in yamlData.keys() :
                        yamlData[ paramNameAtDepth[1]] = {}
                    if paramNameAtDepth[2] not in yamlData[paramNameAtDepth[1]].keys() :
                        yamlData[ paramNameAtDepth[1]][paramNameAtDepth[2]] = {}

                    yamlData[paramNameAtDepth[1]][paramNameAtDepth[2]][key] = values
                elif currentDepth == 3:
                    if paramNameAtDepth[1] not in yamlData.keys() :
                        yamlData[ paramNameAtDepth[1]] = {}
                    if paramNameAtDepth[2] not in yamlData[paramNameAtDepth[1]].keys() :
                        yamlData[ paramNameAtDepth[1]][paramNameAtDepth[2]] = {}
                    if paramNameAtDepth[3] not in yamlData[paramNameAtDepth[1]][paramNameAtDepth[2]].keys() :
                        yamlData[ paramNameAtDepth[1]][paramNameAtDepth[2]][paramNameAtDepth[3]] = {}
                    yamlData[paramNameAtDepth[1]][paramNameAtDepth[2]][paramNameAtDepth[3]][key] = values
            file.close()
            return yamlData

    except OSError as err:
        print('ERROR Can not read file:|' + fileName + '|, ' + "OS error: {0}".format(err) + '\n')
        return yamlData

def JAReadConfigFile( configFile, debugLevel ) :
    """
    Read configuration parameters from given config file
    Print error messages if mandatory parameters are not defined
    Assign default values to the optinal parameters

    Parameters passed
        configFile - config file name
        debugLevel - 0 to 4, 0 no debug info, 4 highest level info to print

    Returned values
        True if success, False on error

        global dictionary JAConfigParams is updated with key and associated values
    """
    global JAConfigParams 

    returnStatus = False

    try:
        with open(configFile, "r") as file:
            if ( debugLevel ):
                errorMsg = f"DEBUG-1 JAReadConfigFile() processing config file: |{configFile}|"
                JALogMsg( errorMsg, logFileName, True, True)

            try:
                import yaml
                tempConfigParams = yaml.load(file, Loader=yaml.FullLoader)
                file.close()
            except:
                tempConfigParams = JAYamlLoad(configFile)

            ### extract parameter values applicable to current environment
            for key, value in tempConfigParams['Environment'].items():
                if key == 'All':
                    # if parameters are not yet defined, read the values from this section
                    # values in this section work as default if params are defined for
                    # specific environment
                    JAGatherEnvironmentSpecs(key, value, debugLevel)

                if value.get('HostName') != None:
                    if re.match(value['HostName'], thisHostName):
                        # current hostname match the hostname specified for this environment
                        # read all parameters defined for this environment
                        JAGatherEnvironmentSpecs(key, value, debugLevel)
                        JAConfigParams['Environment'] = key 

            if 'LogFileName' not in JAConfigParams:
                JAConfigParams['LogFileName'] = 'JACloneGrafanaDashboards.log'
            else:
                JAConfigParams['LogFileName'] = tempConfigParams['LogFileName']

            if 'Environment' not in JAConfigParams:
                JAConfigParams['Environment'] = "Unknown"

            ### first process mandatory parameters
            errorMsg = '' 
            if 'GrafanaAPIURL' not in JAConfigParams:
                errorMsg = f"GrafanaAPIURL is not defined for the current environment: {JAConfigParams['Environment']}\n"

            if 'GrafanaAPIKey' not in JAConfigParams:
                errorMsg = errorMsg + f"GrafanaAPIKey is not defined for the current environment: {JAConfigParams['Environment']}"
           
            if errorMsg != '':
                errorMsg = f"ERROR JAReadConfigFile() Not all mandatory parameters defined in config file: {configFile}\n{errorMsg}"
                JALogMsg( errorMsg, logFileName, True, True)
            else:
                ### assign default values if config file does not have anything specified
                if 'SummaryGroups' not in JAConfigParams:
                    JAConfigParams['SummaryGroups'] = ['Hourly','Daily']
                    if debugLevel :
                        errorMsg = f"DEBUG-1 JAReadConfigFile() SummaryGroups not defined for current environment: {JAConfigParams['Environment']}, using default values: {JAConfigParams['SummaryGroups']}"
                        JALogMsg( errorMsg, logFileName, True, True)
                if 'SummaryValueTypes' not in JAConfigParams:
                    JAConfigParams['SummaryValueTypes'] = ['Min', 'Max', 'Mean']
                    if debugLevel :
                        errorMsg = f"DEBUG-1 JAReadConfigFile() SummaryValueTypes not defined for current environment:{JAConfigParams['Environment']}, using default values: {JAConfigParams['SummaryValueTypes']}"
                        JALogMsg( errorMsg, logFileName, True, True)

                if 'SummaryDashboardTimeFrom' not in JAConfigParams:
                    JAConfigParams['SummaryDashboardTimeFrom'] = {"Hourly": "now-3d", "Daily": "now-30d", "Weekly": "now-180d"}
                    if debugLevel :
                        errorMsg = f"DEBUG-1 JAReadConfigFile() SummaryDashboardTimeFrom not defined for current environment:{JAConfigParams['Environment']}, using default values: {JAConfigParams['SummaryDashboardTimeFrom']}"
                        JALogMsg( errorMsg, logFileName, True, True)

                if 'InfluxdbBucketRetentionRules' not in JAConfigParams:
                    ### 90 days for hourly, one year for daily and 3 years for weekly summary buckets
                    JAConfigParams['InfluxdbBucketRetentionRules'] = {"Hourly": 90, "Daily": 365, "Weekly": 1095}
                    if debugLevel :
                        errorMsg = f"DEBUG-1 JAReadConfigFile() InfluxdbBucketRetentionRules not defined for current environment:{JAConfigParams['Environment']}, using default values: {JAConfigParams['InfluxdbBucketRetentionRules']}"
                        JALogMsg( errorMsg, logFileName, True, True)

                if 'IncludeBuckets' not in JAConfigParams:
                    JAConfigParams['IncludeBuckets'] = 'all'
                    if debugLevel :
                        errorMsg = f"DEBUG-1 JAReadConfigFile() 'IncludeBuckets' not defined for current environment: {JAConfigParams['Environment']}, using default values: {JAConfigParams['IncludeBuckets']}"
                        JALogMsg( errorMsg, logFileName, True, True)
                else:
                    if( JAConfigParams['IncludeBuckets'] == 'All' or JAConfigParams['IncludeBuckets'] == 'ALL' ):
                        JAConfigParams['IncludeBuckets'] = 'all'

                if 'ExcludeBuckets' not in JAConfigParams:
                    JAConfigParams['ExcludeBuckets'] = 'none'
                    if debugLevel :
                        errorMsg = f"DEBUG-1 JAReadConfigFile() 'ExcludeBuckets' not defined for current environment: {JAConfigParams['Environment']}, using default values: {JAConfigParams['ExcludeBuckets']}"
                        JALogMsg( errorMsg, logFileName, True, True)
                else:
                    if( JAConfigParams['ExcludeBuckets'] == 'None' or JAConfigParams['ExcludeBuckets'] == 'NONE' ):
                        JAConfigParams['ExcludeBuckets'] = 'none'

                returnStatus = True

    except OSError as err:
        JAExit('ERROR - Can not open configFile:|' +
                configFile + '|' + "OS error: {0}".format(err) + '\n')

    return returnStatus

if __name__ == '__main__':
    """
    Main program that accepts the command level parameters and performs the actions
    """
    signal.signal(signal.SIGINT, JASignalHandler)

   # parse arguments passed from command line
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-D", type=int, help="debug level 0 - None, 1,2,4-highest level")
    parser.add_argument(
        "-c", help="yml or yaml config file, default - ./JACloneGrafanaDashboards.yml")
    parser.add_argument(
        "-v", action='store_true', help="display version")
    args = parser.parse_args()
    if args.D:
        debugLevel = args.D

    if args.c:
        configFile = args.c
    else:
        configFile = "JACloneGrafanaDashboards.yml"

    if args.v:
        errorMsg = f"JACloneGrafanaDashboards.py version: {JAVersion}"
        JALogMsg( errorMsg, logFileName, True, True)
        sys.exit()

    ### read config file
    if ( JAReadConfigFile( configFile, debugLevel ) == False ) :
        JAExit("FATAL ERROR, exiting")

    if ( 'DebugLevel' in JAConfigParams ) :
        if( debugLevel == 0 ):
            debugLevel = JAConfigParams['DebugLevel']

    if( 'LogFileName' in JAConfigParams):
        logFileName = JAConfigParams['LogFileName']

    regexSearchForDashboardTitle = JAPrepareSearchForDashboardTitle(
            JAConfigParams['SummaryGroups'], 
            JAConfigParams['SummaryValueTypes'], 
            debugLevel)

    ### if manage buckets is enabled, handle it.
    if ( 'InfluxdbManageSummaryBuckets' in JAConfigParams):
        if ( JAConfigParams['InfluxdbManageSummaryBuckets'] == True ):
            returnStatus =     JAManageInfluxdbBuckets( )
            if ( returnStatus == False):
                JAExit("FATAL ERROR")

    ### get uid of existing summary dashboards and create summaryDashboardsHash
    if( JAManageDashboards(True) == False ):
        JAExit("FATAL ERROR")

    ### process non-summary dashboards
    JAManageDashboards(False)

    errorMsg = '\nSummary Stats\n'
    for key in JAStats:
        errorMsg = errorMsg + "\t%32s: %3s\n" % ( key, JAStats[key])  
    JAExit(errorMsg)

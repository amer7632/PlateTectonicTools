"""
    Copyright (C) 2023 The University of Sydney, Australia
    
    This program is free software; you can redistribute it and/or modify it under
    the terms of the GNU General Public License, version 2, as published by
    the Free Software Foundation.
    
    This program is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
    FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
    for more details.
    
    You should have received a copy of the GNU General Public License along
    with this program; if not, write to Free Software Foundation, Inc.,
    51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""


from  collections import deque
import math
import os
import os.path
from .utils import points_in_polygons
from .utils import points_spatial_tree
from .utils import proximity_query
import pygplates
import sys
import numpy as np


#############################################################################################################
# Calculate continent contours, and fragmentation index (global perimeter-to-area ratio), at various times.
#
# TODO: Replace the internal uniform lat/lon sampling with a sampling that’s uniform across the sphere
#       (so that the contour resolution doesn’t favour contours near the North/South poles).
#############################################################################################################
   

class ContouredContinent(object):
    """
    Class to represent the contour around overlapping/abutting continental blocks.
    """
    
    def __init__(self):
        self._polygons_including_continent = []
        self._polygons_excluding_continent = []
    

    def add_polygon(self, polygon, polygon_inside_is_continent):
        """
        Add a polygon contour and whether the inside of the polygon represents continental crust.

        If the *outside* of the polygon represents continental crust then it represents an interior hole in the
        contoured continent or at least an area that is not continental (eg, half the globe is not continental).
        """
        if polygon_inside_is_continent:
            self._polygons_including_continent.append(polygon)
        else:
            self._polygons_excluding_continent.append(polygon)
    

    def get_polygons(self):
        """The polygon contours representing the boundary of this contoured continent."""
        polygons = []

        # Add contour polygons regardless of whether include or exclude continent.
        polygons.extend(self._polygons_including_continent)
        polygons.extend(self._polygons_excluding_continent)

        return polygons
    

    def are_points_inside(self, points, points_spatial_tree=None):
        """Returns a list with same length as 'points' (and in same order) containing True for each point inside this contoured continent."""

        # Improve efficiency by re-using spatial tree of points if caller provides it (otherwise create our own).
        if not points_spatial_tree:
            points_spatial_tree = points_spatial_tree.PointsSpatialTree(points)

        inclusive_polygons_containing_points = points_in_polygons.find_polygons_using_points_spatial_tree(
                points,
                points_spatial_tree,
                self._polygons_including_continent,
                all_polygons=True)
        exclusive_polygons_containing_points = points_in_polygons.find_polygons_using_points_spatial_tree(
                points,
                points_spatial_tree,
                self._polygons_excluding_continent,
                all_polygons=True)
        
        # By default all points are considered inside this contoured continents unless proven otherwise.
        points_inside = [True] * len(points)
        for point_index in range(len(points)):

            # Normally there is just one polygon that includes continent (and zero or more that exclude), and we simply
            # see if the current point is inside the sole inclusive polygon and not inside any exclusive polygons.
            #
            # However with very large contoured continents that are like an annular ring that go right around the globe
            # it's possible to have 0, 2, 3, etc (ie, anything but 1) polygons that include continent.
            # When this happens the inside region of the contoured continent is the *intersection* of these *inclusive* polygons.
            #
            # Also note that the region of an *inclusive* polygon can overlap the regions of one or more *exclusive* polygons.
            # So if the current point is inside all *inclusive* polygons it still doesn't necessarily mean its inside the contoured continent.

            # To be inside, the current point must be inside ALL polygons that INCLUDE continental crust.
            #
            # Note that this contoured continent might not have any polygons that *include* continental crust.
            # This is fine because the contoured region is then the entire globe minus the *excluded* regions.
            # And, in this case, as long as the current point is not inside any excluded regions then it is inside the contoured region.
            inclusive_polygon_indices = inclusive_polygons_containing_points[point_index]
            num_inclusive_polygons_containing_point = len(inclusive_polygon_indices) if inclusive_polygon_indices else 0
            if num_inclusive_polygons_containing_point != len(self._polygons_including_continent):
                points_inside[point_index] = False
                continue

            # To be inside, the current point must be NOT be inside ANY polygon that EXCLUDES continental crust.
            exclusive_polygon_indices = exclusive_polygons_containing_points[point_index]
            num_exclusive_polygons_containing_point = len(exclusive_polygon_indices) if exclusive_polygon_indices else 0
            if num_exclusive_polygons_containing_point != 0:
                points_inside[point_index] = False
                continue
        
        return points_inside


    def get_perimeter(self):
        """Sum of the contours surrounding this contoured continent (in radians)."""
        perimeter = 0.0

        # Add contour perimeters regardless of whether contour includes or excludes continent.
        for polygon in self._polygons_including_continent:
                perimeter += polygon.get_arc_length()
        for polygon in self._polygons_excluding_continent:
                perimeter += polygon.get_arc_length()
        
        return perimeter
    

    def get_area(self):
        """
        The area of this contoured continent (in steradians).
        """
        area = 0.0

        # Add the areas of polygons that include continent and subtract areas of polygons that exclude continent.
        for polygon in self._polygons_including_continent:
                area += polygon.get_area()
        for polygon in self._polygons_excluding_continent:
                area -= polygon.get_area()
        
        # Normally there is just one polygon that includes continent (and zero or more that exclude), and
        # we simply take the area of that one inclusive polygon and substract the areas of the exclusive polygons.
        # In this case the following adds zero area.
        #
        # However with very large contoured continents that are like an annular ring that go right around the globe
        # it's possible to have 0, 2, 3, etc (ie, anything but 1) polygons that include continent.
        # And this is what the following term takes into account. Essentially we need to offset the area by a multiple
        # of the area of the globe (4*pi steradians). When 0 polygons include continent then we have only exclusive areas
        # and so we need to subtract them from the area of the globe (hence the following term becomes 4*pi).
        # When 2 polygons include continent then we need to subtract the area of the globe (hence -4*pi) and when
        # 3 polygons include continent then we need to subtract twice the area of the globe (hence -8*pi).
        # This was determined by drawing up a few examples to see the pattern.
        area -= 4 * math.pi * (len(self._polygons_including_continent) - 1)

        return area


    def get_perimeter_area_ratio(self):
        """The perimeter divided by the area (in units of 1/radians)."""
        return self.get_perimeter() / self.get_area()


class ContinentContouring(object):
    """
    Class to calculate continent contours, and fragmentation index (global perimeter-to-area ratio), at various times.
    """
    
    def __init__(
            self,
            rotaton_model_or_features,
            continent_features,
            # Point spacing used when creating continent contours...
            continent_contouring_point_spacing_degrees,
            # Function (accepting time in Ma) to return area threshold (in square radians) when creating continent contours...
            continent_contouring_area_threshold_steradians_function,
            # Function (accepting time in Ma) to return distance threshold to ensure small gaps between continents are ignored during contouring...
            continent_contouring_gap_threshold_radians_function):
        
        self.rotation_model = pygplates.RotationModel(rotaton_model_or_features)
        self.continent_features = continent_features

        # A point grid to calculate contour polygons representing the boundary of reconstructed static polygons that overlap each other.
        self.contouring_area_threshold_steradians_function = continent_contouring_area_threshold_steradians_function
        self.continent_contouring_gap_threshold_radians_function = continent_contouring_gap_threshold_radians_function

        # The number of latitudes (including -90 and 90).
        self.contouring_grid_num_latitudes = int(math.ceil(180.0 / continent_contouring_point_spacing_degrees)) + 1
        # The number of longitudes (including -180 and 180).
        self.contouring_grid_num_longitudes = 2 * (self.contouring_grid_num_latitudes - 1) + 1

        self.contouring_point_spacing_degrees = 180.0 / (self.contouring_grid_num_latitudes - 1)

        # NOTE: We must generate points on the dateline (ie, at both longitude -180 and 180) since the
        #       contouring alorithm depends on this. We also generate points at the North and South poles
        #       for the same reason.
        lats = np.linspace(-90.0, 90.0, self.contouring_grid_num_latitudes)
        lons = np.linspace(-180.0, 180.0, self.contouring_grid_num_longitudes)

        # Create a multipoint grid of points ordered by longitude first then latitude.
        contouring_longitude_array, contouring_latitude_array = np.meshgrid(lons, lats)
        self.contouring_points = pygplates.MultiPointOnSphere(
                zip(contouring_latitude_array.flatten(), contouring_longitude_array.flatten()))

        # Improve efficiency by re-using spatial tree of contouring points over time (when finding points in polygons and finding points near polygons).
        self.contouring_points_spatial_tree = points_spatial_tree.PointsSpatialTree(self.contouring_points)

        # Debug output contours to GPML.
        self.debug = False
        if self.debug:
            self.debug_contour_polygon_features = []
            self.debug_contouring_inside_point_features = []
    
    
    def __del__(self):
        # Debug output contours to GPML.
        if self.debug:
            pygplates.FeatureCollection(self.debug_contour_polygon_features).write('contour_polygons.gpmlz')
            pygplates.FeatureCollection(self.debug_contouring_inside_point_features).write('contour_inside_points.gpmlz')
    
    
    def get_fragmentation(
            self,
            age):
        """
        Calculate the continental fragmentation index (global perimeter-to-area ratio) at the specified time.
        """

        # Get the contoured continents representing the boundary(s) of the reconstructed continent polygons that overlap each other.
        contoured_continents = self.get_contoured_continents(age)

        total_perimeter = 0.0
        total_area = 0.0
        
        # Update total perimeter and area.
        for contoured_continent in contoured_continents:
            total_perimeter += contoured_continent.get_perimeter()
            total_area += contoured_continent.get_area()

        # Avoid divide-by-zero.
        if total_area == 0.0:
            return 0.0
        
        #print(' global perimeter/area:', total_perimeter / total_area / pygplates.Earth.equatorial_radius_in_kms, 'km-1'); sys.stdout.flush()
        
        #print('age:', age, 'frag_index (1/km):', total_perimeter / total_area / 6371.0); sys.stdout.flush()
        return total_perimeter / total_area
    
    
    def get_contoured_continents(
            self,
            age):
        """
        Reconstruct the continents and then find the boundaries of the specified (potentially overlapping/abutting) continent polygons.
        """
        
        # Reconstruct static continental polygons.
        reconstructed_feature_geometries = []
        pygplates.reconstruct(self.continent_features, self.rotation_model, reconstructed_feature_geometries, age)
        
        # Get a list of polygons.
        #
        # We should have polygons (not polylines) but turn into a polygon if happens to be a polyline
        # (but that actually only works if the polyline is a closed loop and not just part of a polygon's boundary).
        reconstructed_polygons = [pygplates.PolygonOnSphere(reconstructed_feature_geometry.get_reconstructed_geometry())
                for reconstructed_feature_geometry in reconstructed_feature_geometries]
        
        # Calculate contoured continents representing the boundary(s) of the reconstructed continent polygons that overlap each other.
        contoured_continents = self.calculate_contoured_continents(reconstructed_polygons, age)
        
        # Debugging output (if requested).
        if self.debug:

            #
            # Create a debug feature containing all points inside contours.
            #

            contouring_inside_points = []

            # Find the contoured continent (if any) containing each contouring point.
            for contoured_continent_index, contoured_continent in enumerate(contoured_continents):
                # See if any contouring points are inside the current contoured continent.
                contouring_points_inside_contoured_continent = contoured_continent.are_points_inside(self.contouring_points, self.contouring_points_spatial_tree)
                # Record the contoured continent index for any contouring points that are inside it.
                for contouring_point_index, contouring_point_inside in enumerate(contouring_points_inside_contoured_continent):
                    if contouring_point_inside:
                        contouring_inside_points.append(self.contouring_points[contouring_point_index])
            
            contouring_inside_points_feature = pygplates.Feature.create_reconstructable_feature(
                                        pygplates.FeatureType.gpml_unclassified_feature,
                                        pygplates.MultiPointOnSphere(contouring_inside_points),
                                        valid_time=(age + 0.5, age - 0.5))
            self.debug_contouring_inside_point_features.append(contouring_inside_points_feature)

            #
            # Output debug contour polygon features.
            #

            print('time:', age)

            for contoured_continent_index, contoured_continent in enumerate(contoured_continents):
                self.debug_contour_polygon_features.extend(
                        pygplates.Feature.create_reconstructable_feature(
                                pygplates.FeatureType.gpml_unclassified_feature,
                                polygon,
                                valid_time=(age + 0.5, age - 0.5))
                        for polygon in contoured_continent.get_polygons())

                print('  {} perimeter/area:'.format(contoured_continent_index), contoured_continent.get_perimeter() / contoured_continent.get_area() / pygplates.Earth.equatorial_radius_in_kms, 'km-1')
                print('  {} area:'.format(contoured_continent_index), contoured_continent.get_area() * pygplates.Earth.equatorial_radius_in_kms * pygplates.Earth.equatorial_radius_in_kms, 'km')
                print('  {} perimeter:'.format(contoured_continent_index), contoured_continent.get_perimeter() * pygplates.Earth.equatorial_radius_in_kms, 'km')
            
            sys.stdout.flush()
        
        return contoured_continents

    
    def calculate_contoured_continents(
            self,
            continent_polygons,
            age):
        """
        Find the boundaries of the specified (potentially overlapping/abutting) continent polygons as contour polygons.

        The 'age' is only used to look up the time-dependent contouring thresholds (using functions passed into constructor).
        """

        # Get the time-dependent thresholds.
        contouring_area_threshold_steradians = self.contouring_area_threshold_steradians_function(age)
        continent_contouring_gap_threshold_radians = self.continent_contouring_gap_threshold_radians_function(age)

        # Find the reconstructed continental polygon (if any) containing each point.
        continent_polygons_containing_points = points_in_polygons.find_polygons_using_points_spatial_tree(
                self.contouring_points,
                self.contouring_points_spatial_tree,
                continent_polygons)

        # Find the reconstructed continental polygon (if any) near each point.
        # If a point is outside all polygon but close enough to the outline of a polygon then it's considered inside a contour.
        # This ensures small gaps between continents are ignored during contouring.
        continent_polygons_near_points = proximity_query.find_closest_geometries_to_points_using_points_spatial_tree(
                self.contouring_points,
                self.contouring_points_spatial_tree,
                continent_polygons,
                distance_threshold_radians=continent_contouring_gap_threshold_radians)

        #
        # Determine which points are inside contours (and which are outside).
        #
        points_inside_contour = []
        for contouring_point_index in range(len(self.contouring_points)):
            continent_polygon_containing_point = continent_polygons_containing_points[contouring_point_index]
            continent_polygon_near_point = continent_polygons_near_points[contouring_point_index]

            # If the current point is either inside a continent polygon or close enough to its outline
            # then mark the point as inside a contour.
            if (continent_polygon_containing_point is not None or
                continent_polygon_near_point is not None):
                points_inside_contour.append(True)
            else:
                points_inside_contour.append(False)
        
        num_latitudes = self.contouring_grid_num_latitudes
        num_longitudes = self.contouring_grid_num_longitudes

        num_latitude_intervals = num_latitudes - 1
        num_longitude_intervals = num_longitudes - 1

        #
        # Use the Marching Squares algorithm.
        #
        # This is a 2D version (surface of the globe) of the 3D Marching Cubes algorithm.
        # However the main difference between this and using skimage.measure.find_contours(),
        # that we used previously and that also uses the Marching Squares algorithm, is we wrap across the
        # dateline and handle the poles. In this way we avoid contour polygons clamped to the dateline.
        #
        # The way we handle wrapping around the dateline is to have grid points on the dateline (ie, at both longitude -180 and 180).
        # This way lat/lon points on the left side of uniform lat/lon grid of points actually map to the same points on the globe
        # as the lat/lon points on the right side of the uniform lat/lon grid of points, and so they will generated the same
        # point-in-continent-polygon and point-near-continent-polygon results. This ensures the Marching Squares algorithm (below)
        # will produce continuous contour segments across the dateline (as we move from a square on one side of the dateline to the
        # adjacent square on the other side).
        #
        # We also handle the poles correctly by having the bottom row of lat/lon points map to the South pole and the top row
        # map to the North pole. Because all points in a (top or bottom) row map to the same point (pole) on the globe they will
        # generate the same point-in-continent-polygon and point-near-continent-polygon results. And because the entire row is either
        # inside or outside a contour the Marching Squares algorithm (below) cannot generate contour segments that penetrate the row.
        # This essentially avoids the problem at the poles.
        #

        #
        # First find those latitude/longitude squares (each square has 4 points from uniform lat/lon grid of points)
        # that have some of its 4 points inside a contour and some outside. These are squares that will contain an edge (segment)
        # of a contour polygon. According to the Marching Squares algorithm there are 16 cases. Two of these have all 4 points
        # either inside or outside (and hence have no segments). Twelve cases have a single segment. And two cases have two segments
        # (because two diagonals points are inside and the other two diagonal points are outside) - here we can choose to either
        # join two separated contour islands or keep them separated - in our situation we choose to keep them separated.
        # Each segment starts at the middle of one side of the square and ends at the middle of another side.
        # Each side of the square is given an identifier...
        #
        #    ---2---
        #   |       |
        #   1       3
        #   |       |
        #    ---0---
        #
        # ...and each segment records a start and end identifier as a 2-tuple.
        #

        # Records the segments contained by all squares.
        marching_squares = []
        # Records the lat/lon index of only those squares containing one (or two) segments.
        marching_squares_containing_segments = set()
        for latitude_index in range(num_latitude_intervals):
            for longitude_index in range(num_longitude_intervals):

                # See which 4 points of the current square are inside a contour.
                bottom_left_square_inside_contour = points_inside_contour[latitude_index * num_longitudes + longitude_index]
                bottom_right_square_inside_contour = points_inside_contour[latitude_index * num_longitudes + (longitude_index + 1)]
                top_left_square_inside_contour = points_inside_contour[(latitude_index + 1) * num_longitudes + longitude_index]
                top_right_square_inside_contour = points_inside_contour[(latitude_index + 1) * num_longitudes + (longitude_index + 1)]

                # Handle the 16 cases of segments in a square.
                #
                # Store 2 segments (most of the time only 1 is needed).
                # Each segment stores a segment start and end identifier.
                if bottom_left_square_inside_contour:
                    if bottom_right_square_inside_contour:
                        if top_left_square_inside_contour:
                            if top_right_square_inside_contour:
                                segments_in_square = None, None
                            else:
                                segments_in_square = (2,3), None
                        else:
                            if top_right_square_inside_contour:
                                segments_in_square = (1,2), None
                            else:
                                segments_in_square = (1,3), None
                    else:
                        if top_left_square_inside_contour:
                            if top_right_square_inside_contour:
                                segments_in_square = (0,3), None
                            else:
                                segments_in_square = (0,2), None
                        else:
                            if top_right_square_inside_contour:
                                # Choose 2 segments that *do* join two islands.
                                segments_in_square = (0,3), (1,2)
                            else:
                                segments_in_square = (0,1), None
                else:
                    if bottom_right_square_inside_contour:
                        if top_left_square_inside_contour:
                            if top_right_square_inside_contour:
                                segments_in_square = (0,1), None
                            else:
                                # Choose 2 segments that *do* join two islands.
                                segments_in_square = (0,1), (2,3)
                        else:
                            if top_right_square_inside_contour:
                                segments_in_square = (0,2), None
                            else:
                                segments_in_square = (0,3), None
                    else:
                        if top_left_square_inside_contour:
                            if top_right_square_inside_contour:
                                segments_in_square = (1,3), None
                            else:
                                segments_in_square = (1,2), None
                        else:
                            if top_right_square_inside_contour:
                                segments_in_square = (2,3), None
                            else:
                                segments_in_square = None, None
                
                # If current square contains a segment then record that.
                if segments_in_square[0]:
                    marching_squares_containing_segments.add((latitude_index, longitude_index))

                marching_squares.append(segments_in_square)
        
        # Mark those points *inside* any contour as requiring a visit.
        # We also need to remove them once they've been visited.
        points_inside_all_contoured_continents_to_visit = set()
        for latitude_index in range(num_latitudes):
            for longitude_index in range(num_longitudes):
                if points_inside_contour[latitude_index * num_longitudes + longitude_index]:
                    points_inside_all_contoured_continents_to_visit.add((latitude_index, longitude_index))
        
        #
        # Generate contoured continents.
        #
        # Each contoured continent is found by picking an arbitrary point inside any contours and expanding around it until we've filled
        # the entire contoured continent. As we expand we detect when we reach a contour that has not yet been generated and generate it
        # for the current contoured continent. This expanding fill can detect more than one contour per contoured continent.
        # If any contour has an area below the area threshold then we ignore that contour.
        #
        # This is repeated to find all contoured continents (at which time we will have no more points left to visit inside contours).
        #
        contoured_continents = []
        # Keep visting points *inside* any contour until there are no points left to visit.
        while points_inside_all_contoured_continents_to_visit:
            contoured_continent = ContouredContinent()

            # Keep a queue of points inside the current ContouredContinent that we will search for contours.
            points_inside_contoured_continent = deque()

            # Get any available point (inside any contoured continent).
            # This will be the first point inside the current ContouredContinent.
            points_inside_contoured_continent.append(
                points_inside_all_contoured_continents_to_visit.pop())

            # Find the remaining points inside the current ContouredContinent by recursively searching
            # nearbouring points until we reach the contour boundary of the current ContouredContinent.
            while points_inside_contoured_continent:
                # Pop the current point to visit.
                latitude_index, longitude_index = points_inside_contoured_continent.popleft()
                point_index = latitude_index * num_longitudes + longitude_index

                # Search the four squares, adjacent to the current point, for a contour.
                #
                # Note that, for an adjacent square containing a contour, we might already have generated
                # the contour in which case all segments of that contour will have been removed from
                # 'marching_squares_containing_segments' and hence we will be essentially searching for the
                # next contour (if any) of the current contoured continent (eg, an interior hole contour).
                #
                #  +--+--+
                #  |  |  |
                #  +--o--+
                #  |  |  |
                #  +--+--+
                #

                if latitude_index > 0:
                    if longitude_index > 0:
                        neighbour_square_location = latitude_index - 1, longitude_index - 1
                        if neighbour_square_location in marching_squares_containing_segments:
                            self._add_contour_polygon_to_contoured_continent(
                                contoured_continent, self.contouring_points[point_index], neighbour_square_location,
                                marching_squares, marching_squares_containing_segments, contouring_area_threshold_steradians)
                    
                    if longitude_index < num_longitudes - 1:
                        neighbour_square_location = latitude_index - 1, longitude_index
                        if neighbour_square_location in marching_squares_containing_segments:
                            self._add_contour_polygon_to_contoured_continent(
                                contoured_continent, self.contouring_points[point_index], neighbour_square_location,
                                marching_squares, marching_squares_containing_segments, contouring_area_threshold_steradians)

                if latitude_index < num_latitude_intervals - 1:
                    if longitude_index > 0:
                        neighbour_square_location = latitude_index, longitude_index - 1
                        if neighbour_square_location in marching_squares_containing_segments:
                            self._add_contour_polygon_to_contoured_continent(
                                contoured_continent, self.contouring_points[point_index], neighbour_square_location,
                                marching_squares, marching_squares_containing_segments, contouring_area_threshold_steradians)
                    
                    if longitude_index < num_longitudes - 1:
                        neighbour_square_location = latitude_index, longitude_index
                        if neighbour_square_location in marching_squares_containing_segments:
                            self._add_contour_polygon_to_contoured_continent(
                                contoured_continent, self.contouring_points[point_index], neighbour_square_location,
                                marching_squares, marching_squares_containing_segments, contouring_area_threshold_steradians)

                #
                # Propagate outwards from current point to progressively fill the inside of the current contoured continent.
                #
                # This requires visiting up to 8 neighbour points (the '+' in diagram below).
                # Only visit those points that are inside (the contour) and that have not yet been visited.
                #
                #  +--+--+
                #  |  |  |
                #  +--o--+
                #  |  |  |
                #  +--+--+
                #
                # However we don't wrap around the dateline (longitude) and we don't traverse beyond the poles (latitude).
                # We don't need to wrap around dateline because a point at longitude -180 will search the two squares on its right
                # and a point at longitude +180 will search the two squares on its left.
                #

                if latitude_index > 0:
                    neighbour_point_location = latitude_index - 1, longitude_index
                    if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                        points_inside_contoured_continent.append(neighbour_point_location)
                        points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)

                    if longitude_index > 0:
                        neighbour_point_location = latitude_index - 1, longitude_index - 1
                        if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                            points_inside_contoured_continent.append(neighbour_point_location)
                            points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)
                    
                    if longitude_index < num_longitudes - 1:
                        neighbour_point_location = latitude_index - 1, longitude_index + 1
                        if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                            points_inside_contoured_continent.append(neighbour_point_location)
                            points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)
                
                if latitude_index < num_latitudes - 1:
                    neighbour_point_location = latitude_index + 1, longitude_index
                    if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                        points_inside_contoured_continent.append(neighbour_point_location)
                        points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)

                    if longitude_index > 0:
                        neighbour_point_location = latitude_index + 1, longitude_index - 1
                        if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                            points_inside_contoured_continent.append(neighbour_point_location)
                            points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)

                    if longitude_index < num_longitudes - 1:
                        neighbour_point_location = latitude_index + 1, longitude_index + 1
                        if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                            points_inside_contoured_continent.append(neighbour_point_location)
                            points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)

                if longitude_index > 0:
                    neighbour_point_location = latitude_index, longitude_index - 1
                    if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                        points_inside_contoured_continent.append(neighbour_point_location)
                        points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)

                if longitude_index < num_longitudes - 1:
                    neighbour_point_location = latitude_index, longitude_index + 1
                    if neighbour_point_location in points_inside_all_contoured_continents_to_visit:
                        points_inside_contoured_continent.append(neighbour_point_location)
                        points_inside_all_contoured_continents_to_visit.remove(neighbour_point_location)

            # If the contoured continent has one or more contour polygons then add it to the list.
            # Otherwise all its contour polygons had areas below the area threshold.
            if contoured_continent.get_polygons():
                contoured_continents.append(contoured_continent)
        
        return contoured_continents


    def _add_contour_polygon_to_contoured_continent(
            self,
            contoured_continent,
            any_point_inside_contoured_continent,
            first_contour_segment_lat_lon_indices,
            marching_squares,
            marching_squares_containing_segments,
            contouring_area_threshold_steradians):
        """
        Extract a contour polygon and add it to the contoured continent if its area is above the area threshold.
        """

        # First extract the contour polygon (starting at the first segment given to us).
        contour_polygon = self._extract_contour_polygon(
            first_contour_segment_lat_lon_indices,
            marching_squares,
            marching_squares_containing_segments)

        # Exclude contour polygon if its area is smaller than the threshold.
        if contour_polygon.get_area() < contouring_area_threshold_steradians:
            return
        
        # A point inside the contoured continent might actually be outside the current contour polygon (eg, if it's an interior hole).
        # So determine whether this is the case or not (since the contoured continent needs to know this when it's asked to do point-in-polygon tests).
        contour_polygon_inside_is_continent = contour_polygon.is_point_in_polygon(any_point_inside_contoured_continent)
        contoured_continent.add_polygon(contour_polygon, contour_polygon_inside_is_continent)


    def _extract_contour_polygon(
            self,
            first_segment_lat_lon_indices,
            marching_squares,
            marching_squares_containing_segments):
        """
        Follow the segment in marching square at specified lat/lon index around contour back to that first segment.

        Note that a marching square can have two segments, in which case that square represents a thin connecting region between
        two larger islands of the polygon (but still all just one polygon). That square will get traversed twice (once in one direction
        through one segment and once in another the opposite direction through the second segment of that square).
        """

        contour_points = []

        interval_spacing = self.contouring_point_spacing_degrees
        num_latitude_intervals = self.contouring_grid_num_latitudes - 1
        num_longitude_intervals = self.contouring_grid_num_longitudes - 1

        #
        # When a contour is first encountered (during the caller's expanding fill) a contour polygon is generated by starting at the first square
        # found that contains one (or two) segments, which represents the start of that contour. We then pick one of that square's segments
        # (in most cases it'll only have one segment) and generate the first contour point at the segment's start. We then find the
        # adjacent square to the segment's end (since a segment ends in the middle of a side of the square we can find the adjacent square).
        # We then find the segment in the adjacent square that starts (or ends) at the that point (the previous segment end). The adjacent
        # square may contain two segments in which case we need to find the correct segment (that continues the previous segment).
        # We generate the next contour point at the segment start and continue this process following the contour through segments of squares
        # until we return to the first segment (thus closing the contour loop).
        #

        #
        # Starting at the first segment, follow the segments in a loop until they return to the first segment (thus forming a contour polygon).
        #
        latitude_index, longitude_index = first_segment_lat_lon_indices
        prev_segment_end = None
        while True:

            # Get a segment from the current square.
            segment1, segment2 = marching_squares[latitude_index * num_longitude_intervals + longitude_index]
            # If a square has only one segment then it will be in 'segment1' (not 'segment2').
            if segment1 is None:
                # Shouldn't be able to reach a square that doesn't have any segments (or previously had segments but now has none).
                raise AssertionError('Square has no segments')
            
            # Find a segment in the current square such that the segment start matches the
            # end of the previous segment (in the previous square).
            segment_start, segment_end = segment1
            if prev_segment_end is None:  # first segment of current contour...
                # Mark the start of the contour so that later we know when we've completed the contour.
                first_segment_start = segment_start
            else:
                # Continue from the side of the previous square that contains the end point of the previous segment to the side of
                # the current square that should contain the start point of the current segment (that continues previous segment).
                #
                # The right side of previous square continues to left side of current square.
                # The left side of previous square continues to right side of current square.
                # The top side of previous square continues to bottom side of current square.
                # The bottom side of previous square continues to top side of current square.
                #
                # The adjacency relation means 2->0, 0->2, 3->1 and 1->3...
                #
                #    ---2---
                #   |       |
                #   1       3
                #   |       |
                #    ---0---
                #
                # ...which is satisfied by 2^2->0, 0^2->2, 3^2->1 and 1^2->3 (where '^' is exclusive-or).
                # 
                curr_segment_start = prev_segment_end ^ 0b10

                #
                # Find the right segment (if there's two segments) and reverse the segment if necessary
                # so that previous segment end matches current segment start.
                #
                if curr_segment_start == segment_start:
                    # We're traversing segment in the correct direction.
                    pass
                elif curr_segment_start == segment_end:
                    # Reverse segment direction (swap segment start and end points).
                    segment_start, segment_end = segment_end, segment_start
                else:
                    if segment2:
                        # Segment 1 didn't match so swap it with segment 2 (so it can be used later).
                        segment1, segment2 = segment2, segment1

                        segment_start, segment_end = segment1
                        if curr_segment_start == segment_start:
                            # We're traversing segment in the correct direction.
                            pass
                        elif curr_segment_start == segment_end:
                            # Reverse segment direction (swap segment start and end points).
                            segment_start, segment_end = segment_end, segment_start
                        else:
                            raise AssertionError('Unable to find connecting segment')
                    else:
                        raise AssertionError('Unable to find connecting segment')
            
            # The start position of 'segment'.
            # It will be at the midpoint of a side of the square.
            if segment_start == 0:
                segment_start_latitude = -90.0 + latitude_index * interval_spacing
                segment_start_longitude = -180.0 + (longitude_index + 0.5) * interval_spacing
            elif segment_start == 1:
                segment_start_latitude = -90.0 + (latitude_index + 0.5) * interval_spacing
                segment_start_longitude = -180.0 + longitude_index * interval_spacing
            elif segment_start == 2:
                segment_start_latitude = -90.0 + (latitude_index + 1) * interval_spacing
                segment_start_longitude = -180.0 + (longitude_index + 0.5) * interval_spacing
            else:  # segment_start == 3
                segment_start_latitude = -90.0 + (latitude_index + 0.5) * interval_spacing
                segment_start_longitude = -180.0 + (longitude_index + 1) * interval_spacing

            # Generate a contour point at the start of the current segment.
            contour_point = pygplates.PointOnSphere(segment_start_latitude, segment_start_longitude)
            contour_points.append(contour_point)

            # We've just used 'segment1', so discard it by moving 'segment2' into its position to be used later.
            # And if 'segment2' is None then there are no more segments in current square so discard the entire square.
            marching_squares[latitude_index * num_longitude_intervals + longitude_index] = segment2, None
            if segment2 is None:
                # There are no segments left in the current square, so we're finished with it.
                # Note: This will raise KeyError if not present in 'set'.
                marching_squares_containing_segments.remove((latitude_index, longitude_index))

            # We're moving onto the next segment in the next square.
            prev_segment_end = segment_end

            # Move to the next square connected by the end of the current segment.
            #
            #    ---2---
            #   |       |
            #   1       3
            #   |       |
            #    ---0---
            #
            # As noted above, at each pole there is an entire row of lat/lon grid points that are all either inside or outside a contour.
            # This means the Marching Squares algorithm cannot generate contour segments that penetrate the row. So we should not be able
            # to move beyond the poles.
            #
            # Also as noted above, the both the leftmost and rightmost columns of the lat/lon grid of points will be on the dateline
            # (ie, at both longitude -180 and 180). This means the Marching Squares algorithm will produce continuous contour segments across
            # the dateline (as we move from a square on one side of the dateline to the adjacent square on the other side).
            if prev_segment_end == 0:
                latitude_index -= 1
                if latitude_index < 0:
                    raise AssertionError('Segment entered South Pole')
            elif prev_segment_end == 1:
                longitude_index -= 1
                if longitude_index < 0:
                    # Wrap around the dateline.
                    longitude_index += num_longitude_intervals
            elif prev_segment_end == 2:
                latitude_index += 1
                if latitude_index == num_latitude_intervals:
                    raise AssertionError('Segment entered North Pole')
            else:  # prev_segment_end == 3
                longitude_index += 1
                if longitude_index == num_longitude_intervals:
                    # Wrap around the dateline.
                    longitude_index -= num_longitude_intervals
            
            # See if we're returned to the first square (containing the first segment).
            if first_segment_lat_lon_indices == (latitude_index, longitude_index):
                # And make sure the end of the previous segment matches the start of the first segment.
                # See comment above about adjacency relation for explanatation of exclusive-or.
                if first_segment_start == (prev_segment_end ^ 0b10):
                    # Break out of current contour loop (we've completed the contour).
                    break

        # Generate a contour polygon from the current loop of contour points.
        return pygplates.PolygonOnSphere(contour_points)
